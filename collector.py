"""Adaptive state-of-charge collector.

Runs independently of the web app under launchd so history stays continuous
when the dashboard is closed.

Two rules make this cheap and safe:
  * Every paid vehicle_data call is gated on the cheap, sleep-safe state check,
    because a 408 from a sleeping car is billed like any other request.
  * It never wakes the car. On 2021+ vehicles polling does not prevent sleep --
    only commands do -- so this costs no vampire drain.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

import home
import solar
import tesla
import vehicle
from config import settings
from store import Store
from tesla import TeslaAPIError, TeslaAuthError, TeslaClient, VehicleAsleep

DRIVING = {"D", "R", "N"}


def next_interval(car_state: str, view: dict | None, cfg,
                  solar_engaged: int = 0) -> int:
    """Seconds until the next poll. Pure, so it is testable without a car.

    `solar_engaged` is the solar period in seconds when the controller holds
    the car, else 0. It wins over every other cadence EXCEPT sleep: a sleeping
    car is never polled fast, because the loop cannot act on it anyway.
    """
    if car_state != "online":
        return cfg.poll_asleep
    if solar_engaged:
        return solar_engaged
    if view is None:
        return cfg.poll_idle
    # Driving changes SoC fastest, and a drive is short -- check it first so a
    # car that is both moving and (briefly) charging still samples densely.
    if view.get("shift") in DRIVING:
        return cfg.poll_driving
    if view.get("charging"):
        return cfg.poll_charging
    return cfg.poll_idle


async def poll_once(client: TeslaClient, store: Store, vin: str, cfg):
    """One cycle: cheap state check, then a paid read only if it can succeed."""
    try:
        car_state = (await client.vehicle(vin)).get("state") or "offline"
    except TeslaAPIError as exc:
        _log(f"state check failed: {exc}")
        return "offline", None

    if car_state != "online":
        return car_state, None

    try:
        view = vehicle.derive(await client.vehicle_data(vin))
    except VehicleAsleep:
        # Documented: vehicle_data can 408 even when /vehicles says online.
        return "asleep", None
    except TeslaAPIError as exc:
        _log(f"vehicle_data failed: {exc}")
        return car_state, None

    store.record(view, at_home=home.classify(view, home.load(store._db)))
    return car_state, view


ENGAGED_STATES = {"charging", "grace", "stopped"}


async def _command(client: TeslaClient, vin: str, name: str, **params) -> bool:
    """Issue one signed command. Returns True only when the car accepted it.

    client.command() returns a (status, body) TUPLE, and the proxy answers 200
    with result:false for a refusal. Treating any non-exception as success
    would let the controller integrate against an amps value the car never
    adopted -- the exact failure invariant 2 of the spec exists to prevent.
    """
    try:
        status, body = await client.command(vin, name, params)
    except (TeslaAPIError, TeslaAuthError, httpx.HTTPError, OSError) as exc:
        _log(f"command {name} failed: {exc}")
        return False
    if status != 200:
        _log(f"command {name} rejected: HTTP {status}")
        return False
    result = (body or {}).get("response") or {}
    if result.get("result") is False:
        _log(f"command {name} refused: {result.get('reason')}")
        return False
    return True


async def _restore(client: TeslaClient, db, vin: str, st: dict) -> tuple[bool, bool]:
    """Put the owner's own settings back. Returns (succeeded, commanded).

    Clears dirty ONLY on success. A refused or failed write must leave the
    originals on disk to retry: the car still holds the solar values, and this
    record is the only way back to the owner's.
    """
    ok, commanded = True, False
    if st["original_amps"] is not None:
        commanded = True
        ok &= await _command(client, vin, "set_charging_amps",
                             charging_amps=st["original_amps"])
    if st["original_limit"] is not None:
        commanded = True
        ok &= await _command(client, vin, "set_charge_limit",
                             percent=st["original_limit"])
    if not ok:
        _log("restore refused; leaving dirty set to retry")
        return False, commanded
    solar.save_state(db, vin, dirty=0, original_amps=None, original_limit=None,
                     raised_to=None, engaged_at=None)
    return True, commanded


async def recover(client: TeslaClient, db, vin: str, st: dict, location: str,
                  cfg) -> bool:
    """Crash recovery. Returns True when the state is clean enough to engage.

    Gated on all of: dirty, at home, online, proxy reachable. A failed gate
    leaves dirty SET and returns False -- never cleared by giving up.
    """
    if not st["dirty"]:
        return True
    # to_thread because proxy_up opens a blocking socket. car_routes made
    # exactly this mistake and its review caught it; do not reintroduce it.
    proxy_ok = await asyncio.to_thread(tesla.proxy_up, cfg.proxy_url)
    if not solar.may_restore(st["dirty"], location, True, proxy_ok):
        _log(f"dirty, cannot restore yet (location={location}, proxy={proxy_ok})")
        return False
    ok, _ = await _restore(client, db, vin, st)
    if ok:
        # Reset the machine, not just the flags. _restore() nulls the
        # originals, so a machine left in "charging" resumes on the next tick
        # and goes straight to set_amps -- commanding the car with nothing
        # recorded to restore. The re-arm guard only covers charge_start, so
        # it does not catch this path.
        solar.save_state(db, vin, **solar.machine_fields(solar.Machine()))
        _log(f"recovered: restored amps={st['original_amps']} "
             f"limit={st['original_limit']}, machine reset to idle")
    return ok


async def solar_tick(client: TeslaClient, store_: Store, vin: str,
                     view: dict, cfg, site_id) -> tuple[str, bool]:
    """One control iteration. Returns (resulting state name, whether an amps
    write happened).

    Ordering matters: read the meter, decide, act, log. The tick is logged
    whether or not anything was written, so a quiet loop and a broken loop look
    different in solar_ticks.
    """
    db = store_._db
    conf = solar.load_config(db)
    st = solar.load_state(db, vin)
    home_cfg = home.load(db)
    location = home.classify(view, home_cfg)

    today = datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d")

    # --- crash recovery, before anything else can engage -------------------
    if not await recover(client, db, vin, st, location, cfg):
        return st["state"], False
    st = solar.load_state(db, vin)

    if not conf["enabled"] and st["state"] == "idle":
        return "idle", False

    # --- read the meter ----------------------------------------------------
    count, capped = solar.count_request(db, vin, today)
    if capped:
        _log(f"daily request cap reached ({count}); pausing")
        # No location gate here would command a car whose location we cannot
        # even confirm; "unknown" must freeze exactly as it does in advance().
        if st["state"] != "idle" and location == "home":
            await _restore(client, db, vin, st)
        # Persist idle explicitly -- returning "idle" without writing it left
        # the DB holding a stale "charging" that resumed after midnight
        # rollover with nothing left to restore.
        solar.save_state(db, vin, **solar.machine_fields(solar.Machine()))
        return "idle", False

    if site_id is None:
        _log("no energy site resolved; holding")
        return st["state"], False

    try:
        live = await client._get(
            f"/api/1/energy_sites/{site_id}/live_status", ttl=0)
    except (TeslaAPIError, TeslaAuthError, httpx.HTTPError, OSError) as exc:
        _log(f"live_status failed: {exc}; holding")
        return st["state"], False

    grid_w = live.get("grid_power")
    if grid_w is None:
        _log("grid_power absent; skipping tick")
        return st["state"], False
    grid_w = float(grid_w)

    # --- decide ------------------------------------------------------------
    tun = solar.tunables_from(conf, view.get("amps_max"), view.get("volts"))
    current_a = view.get("amps_actual") or view.get("charge_amps") or tun.min_a
    car_w = solar.car_watts(view, tun.volts)
    surplus_w = solar.surplus_watts(car_w, grid_w)
    decision = solar.control(grid_w, int(current_a), tun)

    # Rebuild the WHOLE machine from the database, not just its state name --
    # the dwell and hysteresis counters are what make it stable across ticks.
    machine = solar.machine_from(st)
    tick = solar.Tick(
        surplus_w=surplus_w, decision=decision, location=location,
        plugged=view.get("charging_state") not in (None, "Disconnected"),
        period_s=conf["period_s"])
    machine, actions = solar.advance(machine, tick, solar.policy_from(conf), tun)

    # Without a known original amps there is nothing to restore to, and both
    # restore paths would silently no-op forever. Refuse to engage rather
    # than record a dirty=1 the controller can never make good on.
    if "charge_start" in actions and view.get("charge_amps") is None:
        _log("charge_amps unknown; refusing to engage without a restorable original")
        machine, actions = solar.machine_from(st), []

    # --- act ---------------------------------------------------------------
    written = None
    restored = False
    for action in actions:
        if action == "restore":
            _, commanded = await _restore(client, db, vin, st)
            restored = restored or commanded
        elif action == "wake":
            # wake_up is a dedicated REST endpoint, NOT a signed command --
            # routing it through the proxy would 400. tesla.py:386.
            try:
                await client.wake_up(vin)
                await asyncio.sleep(5)
            except (TeslaAPIError, TeslaAuthError) as exc:
                _log(f"wake failed: {exc}")
                return st["state"], False
        elif action == "charge_start":
            if st["original_amps"] is None:      # remember BEFORE we change it
                solar.save_state(db, vin, dirty=1,
                                 original_amps=view.get("charge_amps"),
                                 original_limit=view.get("limit"),
                                 engaged_at=int(time.time()))
                st = solar.load_state(db, vin)
            await _command(client, vin, "charge_start")
        elif action == "charge_stop":
            await _command(client, vin, "charge_stop")
        elif action == "set_amps":
            target = tun.min_a if machine.state == "grace" else decision.target_a
            if await _command(client, vin, "set_charging_amps",
                              charging_amps=target):
                written = target

    solar.save_state(db, vin, **solar.machine_fields(machine))
    solar.log_tick(db, vin, ts=int(time.time()), state=machine.state,
                   grid_w=grid_w, solar_w=live.get("solar_power"), car_w=car_w,
                   surplus_w=surplus_w, error_w=decision.error_w,
                   amps_before=int(current_a), amps_target=decision.target_a,
                   amps_written=written, soc=view.get("soc"),
                   import_w=max(grid_w, 0.0), period_s=conf["period_s"])
    _log(f"solar {machine.state} surplus={surplus_w:.0f}W "
         f"amps={current_a}->{written if written is not None else '-'}")
    return machine.state, (written is not None) or restored


async def refresh_view(client: TeslaClient, store_: Store, vin: str):
    """A paid vehicle_data read with no preceding state check.

    Only called when the car is already known awake. Returns None if it turns
    out to be asleep after all -- which is the 408 doing the state check's job
    for free.
    """
    try:
        view = vehicle.derive(await client.vehicle_data(vin))
    except VehicleAsleep:
        return None
    except TeslaAPIError as exc:
        _log(f"vehicle_data failed: {exc}")
        return None
    store_.record(view, at_home=home.classify(view, home.load(store_._db)))
    return view


def should_refresh_view(ticks_since_view: int, refresh_every: int,
                        wrote_last_tick: bool) -> bool:
    """Whether this engaged tick must pay for a vehicle_data read.

    Every avoidable call is $0.002 against a $10/month credit. See spec
    section 1.7.1.
    """
    return wrote_last_tick or ticks_since_view >= refresh_every


async def run(once: bool = False) -> int:
    client = TeslaClient(settings)
    store = Store(settings.db_file)
    try:
        vin = await client.resolve_vin()
    except (TeslaAPIError, TeslaAuthError) as exc:
        _log(f"cannot resolve VIN: {exc}")
        await client.aclose()
        store.close()
        return 1

    _log(f"collecting for {vin}")

    # Resolved once, not once per tick: the site doesn't change mid-process,
    # and re-deriving it every tick was an unbudgeted /api/1/products call
    # every time its 300s cache expired -- ~72 extra requests/day at a 120s
    # period, comparable to the whole saving the call-pattern rule buys.
    site_id = None
    try:
        sites = await client.energy_sites()
        if sites:
            site_id = sites[0]["energy_site_id"]
        else:
            _log("no energy site on this account; solar control cannot run")
    except (TeslaAPIError, TeslaAuthError) as exc:
        _log(f"cannot resolve energy site: {exc}")

    engaged, ticks_since_view, wrote_last_tick = 0, 0, False
    car_state, view = "offline", None
    try:
        while True:
            conf = solar.load_config(store._db)
            st = solar.load_state(store._db, vin)
            solar_wanted = bool(conf["enabled"]) or bool(st["dirty"])

            try:
                if engaged and view is not None:
                    # Awake by definition. Skip the state check, and only pay
                    # for vehicle_data when this tick actually needs it.
                    if should_refresh_view(ticks_since_view,
                                           conf["view_refresh_ticks"],
                                           wrote_last_tick):
                        fresh = await refresh_view(client, store, vin)
                        if fresh is None:
                            car_state, view, engaged = "asleep", None, 0
                        else:
                            view, ticks_since_view = fresh, 0
                    else:
                        ticks_since_view += 1
                else:
                    car_state, view = await poll_once(client, store, vin, settings)
                    ticks_since_view = 0
            except TeslaAuthError as exc:
                # Nothing to retry against; launchd will restart us later.
                _log(f"auth lost: {exc}")
                return 1

            wrote_last_tick = False
            if view is not None and solar_wanted:
                state, wrote_last_tick = await solar_tick(
                    client, store, vin, view, settings, site_id)
                engaged = conf["period_s"] if state in ENGAGED_STATES else 0
            else:
                engaged = 0

            soc = (view or {}).get("soc")
            _log(f"{car_state}" + (f" soc={soc}%" if soc is not None else ""))
            if once:
                return 0
            await asyncio.sleep(next_interval(car_state, view, settings, engaged))
    finally:
        await client.aclose()
        store.close()


def _log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    sys.exit(asyncio.run(run(once=parser.parse_args().once)))
