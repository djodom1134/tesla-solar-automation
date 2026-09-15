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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

import energy
import garage
import green
import home
import meters
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
    the car, else 0. It wins over every other cadence EXCEPT sleep.

    A sleeping car is still never polled fast, but no longer because the loop
    cannot act on it -- solar_tick now reasons from the stored snapshot and
    can wake a plugged-in car for sustained surplus. The reason is now purely
    economic, and the arithmetic is one-sided. Over a 10 h window at ~2 billed
    requests per tick:

        1800 s   20 ticks   $0.08/day    <= this
         900 s   40 ticks   $0.16/day
         600 s   60 ticks   $0.24/day

    Halving the interval halves the worst-case wake latency (60 -> 30 min),
    which recovers maybe 0.5 kWh of the morning ramp -- about $0.04 at the
    measured $0.08/kWh self-consumption spread, against $0.08/day of extra
    requests. Polling faster costs more than the sunshine it catches.
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
    except (TeslaAPIError, httpx.HTTPError, OSError) as exc:
        _log(f"state check failed: {exc}")
        return "offline", None

    if car_state != "online":
        return car_state, None

    try:
        view = vehicle.derive(await client.vehicle_data(vin))
    except VehicleAsleep:
        # Documented: vehicle_data can 408 even when /vehicles says online.
        return "asleep", None
    except (TeslaAPIError, httpx.HTTPError, OSError) as exc:
        _log(f"vehicle_data failed: {exc}")
        return car_state, None

    store.record(view, at_home=home.classify(view, home.load(store._db)))
    return car_state, view


# How long we may go without knowing where a car is and still justify waking
# it for sunshine. Six hours spans a working day: long enough that a car
# parked and asleep since breakfast is still actionable at noon, short enough
# that a car driven away yesterday never is.
#
# Judged against solar.knowledge_age_s, NOT against the snapshot's own age.
# The two are the same only for a car nobody was watching; for one this loop
# has watched sleep without a break, the snapshot stays actionable however
# old it is, because a sleeping car has not been driven anywhere. See
# solar.asleep_confirmed -- measuring the snapshot instead switched the whole
# feature off six hours into every overnight sleep.
SNAPSHOT_MAX_AGE_S = 6 * 3600

# Refusals that mean "already in the state you asked for", keyed by the
# command they are benign FOR. The pairing is the point: `not_charging` from
# charge_stop means the car is stopped, which is what we wanted -- but the
# same string from set_charging_amps means the write did NOT happen, and
# calling that success would let the controller integrate against an amps
# value the car never adopted. That is the invariant _command exists to hold,
# and an earlier draft of this set broke it by listing reasons globally.
ALREADY_DONE_REASONS = {
    "charge_start": {"is_charging"},
    "charge_stop": {"not_charging"},
}

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
        reason = result.get("reason")
        # Some refusals mean "already in the state you asked for", which is
        # success for every purpose this controller has. Treating them as
        # failure is worse than cosmetic: the charge_start rollback then
        # abandons a live charge. Observed 2026-07-29 -- the car was charging
        # at its 5 A floor, charge_start came back `is_charging`, and the
        # controller rolled back to "stopped" and left it there through 2.4 kW
        # of export.
        # Matched as a SUBSTRING: the signing proxy wraps the car's own
        # reason in prose -- the live string is
        # "car could not execute command: is_charging", not "is_charging" --
        # so an equality test silently never fires. That is exactly how the
        # first attempt at this fix deployed and changed nothing.
        text = str(reason or "")
        if any(r in text for r in ALREADY_DONE_REASONS.get(name, ())):
            _log(f"command {name}: already {reason}; treating as done")
            return True
        _log(f"command {name} refused: {reason}")
        return False
    return True


async def _restore(client: TeslaClient, db, vin: str, st: dict,
                   view: dict | None) -> tuple[bool, bool]:
    """Put the owner's own settings back. Returns (succeeded, commanded).

    Clears dirty ONLY on success. A refused or failed write must leave the
    originals on disk to retry: the car still holds the solar values, and this
    record is the only way back to the owner's.

    The charge LIMIT is restored only when the controller actually raised it
    (`raised_to` is set) AND the car still holds that raised value (`view`'s
    current limit == `raised_to`). Rewriting it unconditionally would spend a
    billed command to re-assert a value nothing changed, and would silently
    revert an owner who raised or lowered the limit themselves in the Tesla
    app mid-session. `view` is the caller's most recent vehicle_data view;
    pass None only when no view is available at all, which skips the limit
    restore rather than guessing.
    """
    ok, commanded = True, False
    if st["original_amps"] is not None:
        commanded = True
        ok &= await _command(client, vin, "set_charging_amps",
                             charging_amps=st["original_amps"])
    if view is None:
        if st["original_limit"] is not None:
            _log("no view available; skipping charge-limit restore")
    elif (st["original_limit"] is not None and st["raised_to"] is not None
          and view.get("limit") == st["raised_to"]):
        commanded = True
        ok &= await _command(client, vin, "set_charge_limit",
                             percent=st["original_limit"])
    if not ok:
        _log("restore refused; leaving dirty set to retry")
        return False, commanded
    # The handshake goes with it. We have just written original_amps to the
    # car, so the value it will report from here is ours and not the owner's;
    # a commanded_amps left over from the engagement would let a later tick
    # read that restore as an override and pause the feature over our own
    # command.
    solar.save_state(db, vin, dirty=0, original_amps=None, original_limit=None,
                     raised_to=None, engaged_at=None,
                     commanded_amps=None, commanded_ack=0)
    return True, commanded


async def _release(client: TeslaClient, db, vin: str, st: dict,
                   view: dict | None) -> None:
    """Hand the car back to the owner after they overrode the charge rate.

    A restore with the amps half deliberately removed. _restore() puts the
    owner's ORIGINAL rate back, which is exactly wrong here: the rate they
    just set in the app is their current wish, and rewriting the 48 A we
    recorded at engagement would defeat them just as thoroughly as writing
    our own solar target -- only wearing a more helpful face.

    The charge LIMIT is different, and does get put back, under the same
    condition _restore() applies: only when the controller actually raised it
    and the car still holds the raised value. Undoing our own change is not
    fighting the owner. Leaving it is -- a car left at a 90% limit keeps
    charging past the 80% they chose, at the rate they just chose, which is
    the one outcome nobody asked for.

    Unlike _restore this never retries: `dirty` is cleared whether or not the
    limit write succeeded. There is nothing left to retry FOR -- the amps are
    already the owner's, and a failed limit write is a 10% overshoot, not a
    car held at a rate it was never meant to have. Keeping dirty set would
    instead arm the startup recovery path to write the stale original_amps
    back over them on the next restart.
    """
    if (view is not None and st["original_limit"] is not None
            and st["raised_to"] is not None
            and view.get("limit") == st["raised_to"]):
        await _command(client, vin, "set_charge_limit",
                       percent=st["original_limit"])
    solar.save_state(db, vin, dirty=0, original_amps=None, original_limit=None,
                     raised_to=None, engaged_at=None,
                     commanded_amps=None, commanded_ack=0)


async def recover(client: TeslaClient, db, vin: str, st: dict, location: str,
                  cfg, view: dict | None = None) -> bool:
    """Crash recovery. Returns True when the state is clean enough to engage.

    Gated on all of: dirty, at home, online, proxy reachable. A failed gate
    leaves dirty SET and returns False -- never cleared by giving up.

    Called ONCE per process, at startup -- see run(). Recovery is a
    startup-only action (spec 3.6): `dirty` is the normal, healthy condition
    for the entire duration of an engagement (set on charge_start, cleared
    only on restore), so calling this every tick would see dirty=1 on the
    tick right after an ordinary engagement and restore the owner's settings
    mid-charge -- see C5.
    """
    if not st["dirty"]:
        return True
    # to_thread because proxy_up opens a blocking socket. car_routes made
    # exactly this mistake and its review caught it; do not reintroduce it.
    proxy_ok = await asyncio.to_thread(tesla.proxy_up, cfg.proxy_url)
    if not solar.may_restore(st["dirty"], location, True, proxy_ok):
        _log(f"dirty, cannot restore yet (location={location}, proxy={proxy_ok})")
        return False
    ok, _ = await _restore(client, db, vin, st, view)
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

    # A SLEEPING car returns no view at all (poll_once yields None), which
    # used to end the tick before it began -- so the machine could never reach
    # the `wake` action it already had. Observed 2026-07-28: the car sat
    # plugged in at 38% against a 91% limit all morning while the loop logged
    # 66 asleep ticks and never once looked at the meter.
    #
    # Fall back to the stored snapshot, but only to answer "is this worth
    # waking for". Two guards make that safe: the machine must be idle or
    # stopped (never servo amps against a car we cannot see), and the snapshot
    # must still describe a plugged-in car with headroom -- see
    # solar.sleeping_candidate. The sustained-surplus hold then applies
    # unchanged, so a wake is still earned over several ticks rather than
    # bought on one hopeful reading.
    from_snapshot = False
    if view is None:
        if st["state"] not in ("idle", "stopped"):
            return st["state"], False
        snap = store_.snapshot(vin)
        shadow = snap["view"] if snap else None
        # Age from the last moment we KNEW where this car was, which for a
        # car we have watched sleep without a break is this tick -- not from
        # the snapshot, whose age a sleeping car can only ever add to. See
        # solar.asleep_confirmed for the seventeen hours that cost.
        age_s = solar.knowledge_age_s(time.time(),
                                      snap["ts"] if snap else None,
                                      st["asleep_confirmed_ts"])
        if not solar.sleeping_candidate(shadow, age_s, SNAPSHOT_MAX_AGE_S):
            return st["state"], False
        view = shadow
        from_snapshot = True

    home_cfg = home.load(db)
    location = home.classify(view, home_cfg)

    today = datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d")

    # Crash recovery runs ONCE at process startup (see run()), never here.
    # `dirty` is the normal, healthy condition for the whole duration of an
    # engagement -- calling recover() on every tick sees it set on the tick
    # right after an ordinary charge_start and restores the owner's settings
    # mid-charge, forcing the machine back to idle every other tick. See C5.

    now_ts = time.time()
    is_forcing = solar.forcing(conf, now_ts)

    # The cheap early-out, which force mode must not take: with enabled=0 and
    # the machine idle there is normally nothing to do, but a force is
    # precisely a reason to act with enabled=0.
    if not conf["enabled"] and not is_forcing and st["state"] == "idle":
        return "idle", False

    # --- read the meter ----------------------------------------------------
    count, capped = solar.count_request(db, vin, today)
    if capped:
        _log(f"daily request cap reached ({count}); pausing")
        # No location gate here would command a car whose location we cannot
        # even confirm; "unknown" must freeze exactly as it does in advance().
        if st["state"] != "idle" and location == "home":
            await _restore(client, db, vin, st, view)
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
    except TeslaAPIError as exc:
        if exc.status == 429:
            # Invariant 4 (spec 3.7): lengthen the next interval instead of
            # retrying at period_s. Rate limits are shared with every other
            # app on the account, and Tesla's own limit doesn't throttle --
            # it disables the whole application. count/backoff are persisted
            # (not just held here) so a restart mid-event does not resume
            # hammering; run() reads them back to set the actual sleep.
            count = st["consecutive_429s"] + 1
            backoff = solar.backoff_seconds(count, conf["period_s"], exc.retry_after)
            solar.save_state(db, vin, consecutive_429s=count, backoff_s=backoff)
            _log(f"live_status rate-limited (429); backing off {backoff}s "
                 f"(consecutive={count}, retry_after={exc.retry_after})")
            return st["state"], False
        _log(f"live_status failed: {exc}; holding")
        return st["state"], False
    except (TeslaAuthError, httpx.HTTPError, OSError) as exc:
        _log(f"live_status failed: {exc}; holding")
        return st["state"], False

    if st["consecutive_429s"]:
        # Reset on any successful request (spec 3.7 invariant 4). A live
        # read just succeeded, so whatever rate-limit event was in force has
        # cleared.
        solar.save_state(db, vin, consecutive_429s=0, backoff_s=0)

    grid_w = live.get("grid_power")
    if grid_w is None:
        _log("grid_power absent; skipping tick")
        return st["state"], False
    grid_w = float(grid_w)

    # Spec 5: a frozen meter must never be trusted as evidence of surplus. A
    # gateway stuck at a large NEGATIVE (export) reading keeps error_w
    # positive forever -- the controller ramps to max_a and holds there, and
    # nothing downstream can ever observe the floor breach that would stop
    # it. The current reading is included (this tick's own row is not
    # written yet) alongside the prior logged ticks, newest first.
    recent = [grid_w] + solar.recent_grid_w(db, vin, solar.GRID_STUCK_TICKS - 1)
    if solar.grid_is_stuck(recent):
        _log(f"grid_power stuck at {grid_w:.0f}W for >= {solar.GRID_STUCK_TICKS} "
             "consecutive ticks; holding amps, flagging suspect")
        solar.log_tick(db, vin, ts=int(time.time()), state=st["state"],
                       grid_w=grid_w, solar_w=live.get("solar_power"),
                       soc=view.get("soc"), period_s=conf["period_s"],
                       note="grid_power stuck")
        return st["state"], False

    # --- decide ------------------------------------------------------------
    tun = solar.tunables_from(conf, view.get("amps_max"), view.get("volts"))
    # Anchor the integral law to what the car is ACTUALLY drawing right now.
    # amps_actual is 0 whenever the car is not charging, and 0 is falsy, so
    # falling through to charge_amps anchored every engagement to the owner's
    # STANDING rate (48 A here): control() returned clamp(48 + step, min, max)
    # = 48, the already-holds-this-value guard below suppressed the write, and
    # the car opened the solar charge at full rate into whatever surplus
    # existed -- ~340 Wh of grid import per engagement, reproduced against
    # this collector on 2026-07-27. Idle genuinely IS zero draw, and saying
    # so is precisely what makes grid_w a clean measurement of the house.
    live_now = view.get("charging_state") in solar.LIVE_CHARGING_STATES
    current_a = int(view.get("amps_actual") or 0) if live_now else 0
    car_w = solar.car_watts(view, tun.volts)
    surplus_w = solar.surplus_watts(car_w, grid_w)
    decision = solar.control(grid_w, int(current_a), tun)

    # Rebuild the WHOLE machine from the database, not just its state name --
    # the dwell and hysteresis counters are what make it stable across ticks.
    machine = solar.machine_from(st)
    tick = solar.Tick(
        surplus_w=surplus_w, decision=decision, location=location,
        plugged=view.get("charging_state") not in (None, "Disconnected"),
        car_charging=view.get("charging_state") in solar.LIVE_CHARGING_STATES,
        period_s=conf["period_s"])
    # --- the manual-override pause ----------------------------------------
    #
    # Whether the owner has taken the charge rate back, and whether they have
    # given it up again. Evaluated BEFORE the machine decides anything, so a
    # paused tick never reaches advance() at all -- the same shape force mode
    # uses, and for the same reason: a mode that "runs the machine but
    # suppresses its writes" would keep advancing dwell counters against a
    # car it is not driving and re-engage on some later tick out of nowhere.
    #
    # Force mode is excluded entirely. A force IS the owner asking for
    # maximum rate; force_plan already owns that conversation, and pausing
    # inside it would mean the feature fighting its own override.
    paused = st["override_amps"] is not None
    latched_now = False
    fields: dict = {}
    if conf["pause_on_override"] and not is_forcing:
        if paused:
            armed_next, cleared = solar.override_cleared(
                bool(st["override_armed"]), view.get("charging_state"))
            if cleared:
                paused = False
                fields = {"override_amps": None, "override_since": None,
                          "override_armed": 0,
                          "commanded_amps": None, "commanded_ack": 0}
                _log("charging stopped and started again; solar control resumes")
            elif armed_next != bool(st["override_armed"]):
                fields = {"override_armed": int(armed_next)}
        else:
            ack, override_amps = solar.override_step(
                st["commanded_amps"], bool(st["commanded_ack"]),
                view.get("charge_amps"))
            if override_amps is not None:
                paused = latched_now = True
                fields = {"override_amps": override_amps,
                          "override_since": int(now_ts), "override_armed": 0,
                          "commanded_amps": None, "commanded_ack": 0}
                _log(f"charge rate set to {override_amps}A outside this "
                     "controller; pausing solar control until charging is "
                     "stopped and started again")
            elif ack != bool(st["commanded_ack"]):
                fields = {"commanded_ack": int(ack)}
    if fields:
        solar.save_state(db, vin, **fields)
        st = solar.load_state(db, vin)

    if paused:
        # Hands off. The tick is still LOGGED below -- green.charged_split
        # derives the whole solar/grid attribution from solar_ticks, and a
        # manual charge that went unlogged would fill the pack with grid
        # electrons the banked-solar ledger never accounted for.
        machine, actions = solar.Machine(), []
        if latched_now:
            # The one command the pause may issue, and only on its first
            # tick: putting back a charge limit we ourselves raised. See
            # _release for why the amps are pointedly not restored with it.
            await _release(client, db, vin, st, view)
            st = solar.load_state(db, vin)
    elif is_forcing:
        # The solar machine does not run at all while forcing -- it stays
        # idle, and force_plan decides. Note the tick is still LOGGED below:
        # green.charged_split derives the whole solar/grid attribution from
        # solar_ticks, and skipping the log would make a forced overnight
        # charge invisible to the ledger and to "miles added today".
        actions, started_next = solar.force_plan(
            state=st["state"], location=location, plugged=tick.plugged,
            car_charging=tick.car_charging,
            amps_actual=int(view.get("amps_actual") or 0),
            amps_max=tun.max_a, force_started=bool(st["force_started"]))
        machine = solar.Machine()
        if started_next != bool(st["force_started"]):
            solar.save_state(db, vin, force_started=int(started_next))
        if solar.force_expired(
                force_charge_until=conf["force_charge_until"], now=now_ts,
                car_charging=tick.car_charging,
                force_started=bool(st["force_started"])):
            _log("force charge expired; restoring and returning to solar")
            await _restore(client, db, vin, st, view)
            solar.save_state(db, vin, force_started=0)
            # solar_config is the WEB APP's table (see save_config's comment).
            # This is the one collector write to it, and it is deliberate:
            # nothing else can observe midnight. Keep it to this single field.
            solar.save_config(db, force_charge_until=None)
            actions = []
    else:
        machine, actions = solar.advance(machine, tick, solar.policy_from(conf), tun)

    # Without a known original amps there is nothing to restore to, and both
    # restore paths would silently no-op forever. Refuse to engage rather
    # than record a dirty=1 the controller can never make good on. Applies to
    # BOTH ways into "charging" -- adoption needs its own restorable original
    # exactly as an ordinary charge_start does, or the car's remembered
    # per-location amps setting is lost the moment we touch it.
    if ("charge_start" in actions or "adopt" in actions) and view.get("charge_amps") is None:
        _log("charge_amps unknown; refusing to engage without a restorable original")
        machine, actions = solar.machine_from(st), []

    # --- charge-limit raise (spec 3.4) -------------------------------------
    # BEFORE the action loop, not after, and the ordering is load-bearing.
    # charge_start's refusal path below returns early, so a raise evaluated
    # after it is unreachable on precisely the ticks that need it: a car at
    # its limit refuses charge_start with `complete`, the machine rolls back
    # to "stopped", and the function returns before ever asking whether the
    # limit should go up. That is the deadlock observed 2026-09-13 -- five
    # days at an 85% limit with 4 kW exporting, every tick refusing.
    #
    # Running first also means the raise and the retry land in the SAME tick:
    # set_charge_limit goes out, then charge_start follows against a limit
    # the car has already accepted. If the car has not caught up yet the next
    # tick gets it anyway, so this costs nothing and often saves two minutes.
    target_limit, raise_hold = solar.raise_decision(
        enabled=bool(conf["raise_limit"]),
        state=machine.state,
        soc=view.get("soc"),
        limit=view.get("limit"),
        ceiling=conf["soc_ceiling"],
        grid_w=grid_w,
        raised_to=st["raised_to"],
        hold_elapsed_s=st["raise_hold_elapsed"],
        raise_hold_s=conf["raise_hold_s"],
        period_s=conf["period_s"],
        # "Complete" is the car reporting no headroom, which is the one
        # refusal a higher limit is the cure for. See raise_decision.
        complete=view.get("charging_state") == "Complete",
        plugged=tick.plugged,
        location=location,
    )
    raised = st["raised_to"]
    if target_limit is not None:
        # Remember BEFORE we change it, exactly as charge_start does below.
        # The raise can now fire from "stopped", which is AHEAD of the branch
        # that normally records the originals -- so without this, a
        # set_charge_limit that lands while the following charge_start is
        # still being refused leaves original_limit unset. The next tick then
        # records the RAISED value as the original, and _restore puts back 95
        # instead of the owner's 85. dirty=1 goes with it, or restore never
        # runs at all.
        #
        # original_amps is deliberately left alone: we have not touched amps,
        # and _restore skips the amps write when it is None.
        if st["original_limit"] is None:
            solar.save_state(db, vin, dirty=1, original_limit=view.get("limit"),
                             engaged_at=int(time.time()))
            st = solar.load_state(db, vin)
        if await _command(client, vin, "set_charge_limit", percent=target_limit):
            raised = target_limit
            _log(f"raised charge limit {view.get('limit')} -> {target_limit} "
                 f"for solar")
    solar.save_state(db, vin, raised_to=raised, raise_hold_elapsed=raise_hold)
    st = solar.load_state(db, vin)

    # --- act ---------------------------------------------------------------
    written = None
    restored = False
    for action in actions:
        if action == "restore":
            _, commanded = await _restore(client, db, vin, st, view)
            restored = restored or commanded
        elif action == "wake":
            # Only a car we could not read is actually asleep. advance()
            # emits `wake` on every stopped -> charging transition because it
            # is pure and cannot know, so the decision lands here.
            #
            # This matters more now that restart_hold_s may be 0: a wake is
            # the most expensive request this system can make ($0.02, against
            # $0.001 for a command), and spending one on a car we just read
            # live buys precisely nothing.
            if not from_snapshot:
                continue
            # wake_up is a dedicated REST endpoint, NOT a signed command --
            # routing it through the proxy would 400. tesla.py:386.
            try:
                await client.wake_up(vin)
                await asyncio.sleep(5)
            except (TeslaAPIError, TeslaAuthError, httpx.HTTPError, OSError) as exc:
                _log(f"wake failed: {exc}")
                # Persist a RESET hold, not just an early return. Returning
                # st["state"] unchanged leaves hold_s already at
                # restart_hold_s on disk, so advance() re-emits ["wake", ...]
                # on the very next tick -- at $0.02/wake and a 120s period
                # that is ~$0.60/hour until the daily cap intervenes. Forcing
                # "stopped" with hold_s=0 makes the dwell re-accumulate.
                solar.save_state(db, vin, **solar.machine_fields(
                    solar.Machine(state="stopped", hold_s=0)))
                return "stopped", False
        elif action == "charge_start":
            if st["original_amps"] is None:      # remember BEFORE we change it
                solar.save_state(db, vin, dirty=1,
                                 original_amps=view.get("charge_amps"),
                                 original_limit=view.get("limit"),
                                 engaged_at=int(time.time()))
                st = solar.load_state(db, vin)
            if not await _command(client, vin, "charge_start"):
                # The car did not start. Most often it was still coming out
                # of sleep: a command issued seconds behind wake_up comes
                # back HTTP 500 (observed live 2026-07-28 09:30).
                #
                # Advancing to "charging" anyway strands the machine three
                # ways at once -- it believes it is servoing a charge that
                # does not exist, it writes amps to a car drawing nothing,
                # and because "charging" is neither idle nor stopped the
                # meter watch stops as well, so nothing re-checks for a full
                # poll_asleep. Put the owner's settings back and stay put.
                _log("charge_start refused; rolling back to stopped")
                await _restore(client, db, vin, solar.load_state(db, vin), view)
                solar.save_state(db, vin, **solar.machine_fields(
                    solar.Machine(state="stopped", hold_s=0)))
                return "stopped", False
        elif action == "force_start":
            # Record the restorable original BEFORE touching anything, the
            # same invariant charge_start holds above: without a known
            # original there is nothing to restore to and both restore paths
            # silently no-op forever.
            if view.get("charge_amps") is None:
                _log("charge_amps unknown; refusing to force without a "
                     "restorable original")
                break
            if not st["dirty"]:
                solar.save_state(db, vin, dirty=1,
                                 original_amps=view.get("charge_amps"),
                                 engaged_at=int(now_ts))
                st = solar.load_state(db, vin)
            if not await _command(client, vin, "charge_start"):
                _log("forced charge_start refused")
                break

        elif action == "force_amps":
            await _command(client, vin, "set_charging_amps",
                           charging_amps=tun.max_a)

        elif action == "adopt":
            # Taking over a charge the CAR started, not us -- issue no
            # charge_start (it is already running), but record the originals
            # BEFORE we ever touch amps, exactly as charge_start does above.
            # Skipping this is how three earlier Critical defects on this
            # path began: with nothing recorded, _restore() has nothing to
            # put back and the owner's own per-location amps setting is lost
            # the moment set_amps below writes over it.
            if st["original_amps"] is None:
                solar.save_state(db, vin, dirty=1,
                                 original_amps=view.get("charge_amps"),
                                 original_limit=view.get("limit"),
                                 engaged_at=int(time.time()))
                st = solar.load_state(db, vin)
        elif action == "charge_stop":
            await _command(client, vin, "charge_stop")
        elif action == "set_amps":
            if machine.state == "grace":
                target = tun.min_a
            elif decision.unramped_target_a < current_a:
                # T1.2, generalising what was once an adoption-tick-only
                # bypass: a DOWNWARD correction skips ramp_a entirely, in
                # every state. Reducing draw is always safe, so rationing it
                # buys nothing -- an AC compressor is a ~6 kW step, and at
                # 8 A per tick a car at 48 A spends four more ticks importing
                # its way to a number the meter already reported.
                #
                # Upward moves stay ramp-limited: slamming into a surplus
                # that may not still be there is a real risk. That asymmetry
                # -- fast down, slow up -- is also what keeps the loop
                # stable, since the aggressive direction is the one that can
                # only reduce error.
                target = decision.unramped_target_a
            elif "charge_start" in actions:
                # Spec T0.2. The owner's requirement: hold the rate at zero
                # until the excess is well measured, then converge fast.
                # Both halves are already satisfied here for free -- idle IS
                # zero draw (no command, no contactor cycle, no wake), so
                # with current_a anchored at 0 above, grid_w measured the
                # house EXACTLY and unramped_target_a is the established
                # rate. Ramping toward it 8 A per tick would only import for
                # the eight minutes it took to arrive somewhere already
                # known. Upward bypass is safe on this tick alone, because
                # this is the one tick whose measurement contained no car.
                target = decision.unramped_target_a
            else:
                target = decision.target_a
            # Spec 3.2: never re-send a value the car already holds
            # acknowledged (charge_current_request). Without this, a target
            # pinned at a clamp while the error stays outside the deadband
            # re-commands the same value every tick forever -- most likely
            # triggered by charge completion, where amps_actual drops to 0,
            # surplus_w becomes the full export, and the machine stays
            # charging. Grace entry is the one exception: it must always
            # write, because it is the one permitted ramp violation.
            if target == view.get("charge_amps") and machine.state != "grace":
                continue
            if await _command(client, vin, "set_charging_amps",
                              charging_amps=target):
                written = target
                # Open a fresh handshake on our own command (see
                # solar.override_step). The acknowledgement resets with it:
                # until the car is seen reporting THIS value, the rate it is
                # still reporting is our previous one in transit, not the
                # owner's decision -- and reading it as one would pause the
                # feature on the controller's own write.
                if st["commanded_amps"] != target or st["commanded_ack"]:
                    solar.save_state(db, vin, commanded_amps=target,
                                     commanded_ack=0)
                    st = solar.load_state(db, vin)

    # --- banked-solar ledger (Task 18) and lifetime free miles (Task 20) ----
    # last_tick_ts() must be read BEFORE log_tick() below writes this tick's
    # own row, or MAX(ts) would return this tick and the gap would always
    # read as zero. Skipped entirely when soc is unknown -- nothing to
    # observe, so ledger_soc/solar_soc are left exactly as they were rather
    # than guessed.
    #
    # gap_threshold_s is deliberately NOT store.GAP_SECONDS (the SoC chart's
    # own gap threshold, tuned for when a hole is worth drawing dashed) --
    # see green.ledger_step's docstring. It is derived from this car's own
    # poll cadence instead: poll_asleep/poll_idle can legitimately be as long
    # as 1800s (raised there for API budget reasons), so the ledger needs
    # real headroom above that or ordinary overnight sleep trips it on
    # nothing. Doubled for margin against scheduler jitter, floored at 1h so
    # a very fast poll cadence can't make the threshold silly-small.
    # ONE timestamp for this tick, shared by the ledger bookmark below and
    # log_tick at the end. Two separate time.time() calls can straddle a
    # second boundary, and a ledger_since_ts one second behind this tick's
    # logged ts makes `ts > since_ts` include the tick that has just been
    # credited -- counting its energy again toward the next point.
    tick_ts = int(time.time())
    ledger_fields: dict = {}
    soc_now = view.get("soc")
    if soc_now is not None:
        prev_tick_ts = solar.last_tick_ts(db, vin)
        gap_s = tick_ts - prev_tick_ts if prev_tick_ts is not None else 0
        gap_threshold_s = max(2 * getattr(cfg, "poll_asleep", 1800), 3600)
        # PROPORTIONAL attribution, not all-or-nothing. This used to pass a
        # boolean -- engaged, and any solar at all -- which banked the WHOLE
        # SoC rise as sun. A car drawing 2,000 W against 1,900 W of surplus
        # is 5% utility-powered, and recording that as 100% solar makes the
        # ledger flattering rather than true.
        #
        # INTEGRATED over the whole percentage point, not sampled at the tick
        # that crossed it. SoC is an integer: a point is ~0.82 kWh on this
        # pack, thirteen minutes at 3.6 kW, about seven ticks. Reading the
        # split off tick seven alone threw away the other six -- observed
        # 2026-09-13, where a cloud at the boundary booked a point as pure
        # grid though half of it went in under full sun two minutes earlier.
        # See green.interval_solar_fraction.
        #
        # This tick's own row is not in the log yet (log_tick runs below), so
        # it is appended by hand -- it is the tick that closes the interval
        # and carries as much weight as any other.
        since_ts = st["ledger_since_ts"]
        span = solar.ticks_since(db, vin, since_ts) if since_ts is not None else []
        span.append({"state": machine.state, "car_w": car_w, "grid_w": grid_w,
                     "period_s": conf["period_s"]})
        fraction = green.interval_solar_fraction(span)
        new_solar_soc, ledger_stale = green.ledger_step(
            st["solar_soc"], st["ledger_soc"], int(soc_now), fraction,
            gap_s, gap_threshold_s)
        ledger_fields = {"solar_soc": new_solar_soc, "ledger_soc": int(soc_now),
                         "ledger_stale": 1 if ledger_stale else 0}
        # The bookmark moves only when the SoC actually moved. While it sits
        # still the span keeps widening, which is the whole point: the next
        # point to land gets credited from every tick that fed it.
        if st["ledger_soc"] is None or int(soc_now) != st["ledger_soc"]:
            ledger_fields["ledger_since_ts"] = tick_ts

        # Lifetime solar/grid energy into the car is NOT accumulated here.
        # It is derived from the tick log on read (green.solar_kwh /
        # green.grid_kwh), which this tick's own log_tick below feeds. A
        # counter would start at zero on the day it was added and silently
        # disagree with every history-derived figure beside it -- which is
        # precisely how "charged so far" came to contradict "banked solar".

        # Task 20: lifetime free miles driven -- green.free_miles_step's own
        # "before" arguments are st["solar_soc"]/st["ledger_soc"], the SAME
        # pre-tick values ledger_step was just called with above, not the
        # new_solar_soc this tick just produced (see that function's
        # docstring for why). Independently gated on the odometer being
        # known, same "nothing to observe, don't guess" treatment as soc
        # above -- a view missing odometer_mi leaves free_miles_driven/
        # tracked_miles/ledger_odo exactly as they were.
        odo_now = view.get("odometer_mi")
        if odo_now is not None:
            first_observation = st["ledger_odo"] is None
            new_free_miles, new_tracked_miles, new_ledger_odo = green.free_miles_step(
                st["free_miles_driven"], st["tracked_miles"], st["ledger_odo"],
                float(odo_now), st["solar_soc"], st["ledger_soc"])
            ledger_fields["free_miles_driven"] = new_free_miles
            ledger_fields["tracked_miles"] = new_tracked_miles
            ledger_fields["ledger_odo"] = new_ledger_odo
            if first_observation:
                ledger_fields["free_miles_since"] = int(time.time())

    solar.save_state(db, vin, **solar.machine_fields(machine), **ledger_fields)
    solar.log_tick(db, vin, ts=tick_ts, state=machine.state,
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
    except (TeslaAPIError, httpx.HTTPError, OSError) as exc:
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


# --------------------------------------------------------------------------
# Task 17b: the ratgdo garage opener. Two independent features, both gated on
# their own config and both no-ops when unconfigured:
#   * garage_arrival_tick   -- open-only, triggered by the car crossing INTO
#                              the ring while driving. Runs on the drive path,
#                              already paid for by poll_driving (spec 1.8/17).
#   * garage_scheduled_close_tick -- the owner's late-night schedule, the only
#                              path that ever closes automatically. Runs every
#                              tick regardless of the car's location -- an
#                              open garage with the car gone is worse than one
#                              with the car in it.
# --------------------------------------------------------------------------

async def garage_arrival_tick(db, vin: str, view: dict, cfg: dict, home_cfg) -> None:
    """Auto-open on arrival. Pure decision lives in garage.should_open(); this
    only computes ring membership, loads/saves the one-shot latch, and does
    the I/O.

    The latch is the ONLY thing persisted across ticks here -- there is no
    stored "previous ring membership". should_open() is only ever called
    from the one branch below where the latch is armed AND the car is
    observed inside the ring, which is exactly the condition the latch
    exists to detect: armed means "confirmed outside the ring since the last
    time we fired", so a car found inside the ring while still armed IS the
    transition being latched for. inside_ring_prev is passed as False for
    that reason, not because it is tracked position-by-position.
    """
    if not cfg["garage_auto_open"] or not cfg["garage_url"] or home_cfg is None:
        return
    lat, lon = view.get("lat"), view.get("lon")
    if lat is None or lon is None:
        return  # unknown location -- freeze, same as every other consumer of it

    inside_ring = home.distance_m(lat, lon, home_cfg.latitude,
                                  home_cfg.longitude) <= cfg["garage_ring_m"]
    st = solar.load_state(db, vin)
    armed = bool(st["garage_armed"])

    if not inside_ring:
        if not armed:
            solar.save_state(db, vin, garage_armed=1)  # arm on leaving the ring
        return

    if not armed:
        return  # inside the ring but never confirmed leaving it -- not an arrival

    data = await asyncio.to_thread(garage.status, cfg["garage_url"])
    door_state = (data or {}).get("garageDoorState")
    obstructed = bool((data or {}).get("garageObstructed"))

    if not garage.should_open(door_state=door_state, obstructed=obstructed,
                              armed=armed, inside_ring_now=True,
                              inside_ring_prev=False, shift=view.get("shift")):
        return

    opened = await asyncio.to_thread(garage.open, cfg["garage_url"])
    verify = await asyncio.to_thread(garage.status, cfg["garage_url"])
    solar.save_state(db, vin, garage_armed=0)  # disarm on firing
    _log(f"garage auto-open: open()={opened}, door now "
         f"{(verify or {}).get('garageDoorState', 'unknown')!r}")


async def garage_scheduled_close_tick(db, vin: str, cfg: dict, tz: str) -> None:
    """The owner's late-night schedule -- the only path that ever closes the
    door automatically. See garage.py's module docstring for why there is no
    warned close reachable over the ratgdo's own API, and why this sequence
    (light on, wait, re-read, abort on obstruction or on the door no longer
    being Open) is what approximates it instead.

    Stamps garage_last_close_day BEFORE attempting anything, not after: "once
    per day, whatever happens" means a crash mid-sequence, an unreachable
    device, or a door that was never open must not leave the door retrying
    against a possibly-obstructed door for the rest of the day, including
    across a process restart.
    """
    close_hour = cfg["garage_close_hour"]
    url = cfg["garage_url"]
    if close_hour is None or not url:
        return

    now = datetime.now(ZoneInfo(tz))
    if now.hour != close_hour:
        return

    today = now.strftime("%Y-%m-%d")
    st = solar.load_state(db, vin)
    if st["garage_last_close_day"] == today:
        return
    solar.save_state(db, vin, garage_last_close_day=today)

    data = await asyncio.to_thread(garage.status, url)
    if data is None:
        _log("garage scheduled close: device unreachable, skipping today")
        return
    door_state = data.get("garageDoorState")
    obstructed = bool(data.get("garageObstructed"))
    if not garage.safe_to_close(door_state, obstructed):
        _log(f"garage scheduled close: skipping -- door_state={door_state!r} "
             f"obstructed={obstructed}")
        return

    warn_s = cfg["garage_close_warn_s"]
    _log(f"garage scheduled close: door is Open, warning {warn_s}s (light on) "
         "before re-checking")
    await asyncio.to_thread(garage.light_on, url)
    await asyncio.sleep(warn_s)

    data = await asyncio.to_thread(garage.status, url)
    if data is None:
        _log("garage scheduled close: device unreachable after the warning "
             "wait, aborting -- the second look is the whole point")
        return
    door_state = data.get("garageDoorState")
    obstructed = bool(data.get("garageObstructed"))
    if not garage.safe_to_close(door_state, obstructed):
        _log(f"garage scheduled close: aborting after the wait -- "
             f"door_state={door_state!r} obstructed={obstructed}")
        return

    closed = await asyncio.to_thread(garage.close, url)
    verify = await asyncio.to_thread(garage.status, url)
    _log(f"garage scheduled close: close()={closed}, door now "
         f"{(verify or {}).get('garageDoorState', 'unknown')!r}")



# How often to look for a newly-closed day. Hourly is ample: a day closes
# once, and checking more often just spends requests discovering nothing.
SITE_INGEST_INTERVAL_S = 3600

# A wake is $0.02, 20x a command. A car that refuses to wake must not be
# asked every tick until midnight.
FORCE_WAKE_MIN_S = 300


async def ingest_site_meters(client, db, site_id, cfg, now: float) -> int:
    """Advance the site energy counters by any days that have CLOSED.

    Only whole days strictly before today in the configured timezone are
    ingested. Tesla revises the open bucket downward as data settles, and a
    counter that followed it would either move backwards -- which HA reads as
    a meter swap, zeroing its baseline and then booking the next sample in
    full -- or double-count when the bucket later grew.

    Returns the number of days ingested, which is also the number of billed
    requests made: one per day, normally zero or one.
    """
    if site_id is None:
        return 0
    tz = ZoneInfo(cfg.timezone)
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    yesterday = int((today - timedelta(days=1)).timestamp())
    for ch in meters.CHANNELS:
        meters.seed(db, ch, yesterday)
    last = meters.last_closed_bucket(db, "site_import")

    ingested = 0
    day = datetime.fromtimestamp(last, tz) + timedelta(days=1)
    # Bounded per pass so a long outage cannot spend the whole daily budget
    # catching up in one go.
    while day < today and ingested < 3:
        try:
            hist = await client.calendar_history(
                site_id, "day", day.replace(hour=23, minute=59, second=59),
                cfg.timezone)
        except (TeslaAPIError, TeslaAuthError, httpx.HTTPError, OSError) as exc:
            _log(f"site meter ingest failed for {day:%Y-%m-%d}: {exc}")
            return ingested
        rows = [energy.derive(r) for r in (hist or {}).get("time_series") or []]
        total = energy.summarize(rows)
        stamp = int(day.timestamp())
        meters.close_day(db, "site_import", total.get("grid_import", 0) * 1000, stamp)
        meters.close_day(db, "site_export", total.get("grid_export", 0) * 1000, stamp)
        meters.close_day(db, "site_solar", total.get("solar", 0) * 1000, stamp)
        _log(f"site meters +{day:%Y-%m-%d}: "
             f"import {total.get('grid_import', 0):.1f} kWh, "
             f"export {total.get('grid_export', 0):.1f} kWh, "
             f"solar {total.get('solar', 0):.1f} kWh")
        ingested += 1
        day += timedelta(days=1)

    # Then today's partial, every pass. This is what gives HA hourly shape:
    # without it the counter would sit still all day and then jump a whole
    # day's energy at once, which HA books into a single five-minute bucket.
    try:
        hist = await client.calendar_history(
            site_id, "day", datetime.now(tz), cfg.timezone)
        rows = [energy.derive(r) for r in (hist or {}).get("time_series") or []]
        t = energy.summarize(rows)
        meters.observe_today(db, "site_import", t.get("grid_import", 0) * 1000)
        meters.observe_today(db, "site_export", t.get("grid_export", 0) * 1000)
        meters.observe_today(db, "site_solar", t.get("solar", 0) * 1000)
    except (TeslaAPIError, TeslaAuthError, httpx.HTTPError, OSError) as exc:
        _log(f"site meter today-read failed: {exc}")
    return ingested


async def run(once: bool = False) -> int:
    client = TeslaClient(settings)
    store = Store(settings.db_file)
    engaged, ticks_since_view, wrote_last_tick = 0, 0, False
    car_state, view = "offline", None
    # Crash recovery (spec 3.6) runs ONCE per process, here, not per tick --
    # see C5. A clean start (not dirty) clears this on the very first engaged
    # tick at no cost, because recover() itself returns True immediately when
    # nothing is dirty.
    recovery_done = False
    # What the loop slept before this iteration, so a reader can judge whether
    # a heartbeat is overdue against the cadence actually in force -- which
    # ranges from 120 s engaged to 1800 s after dark.
    last_sleep_s = 60
    # Runs on first pass, then hourly.
    last_site_ingest = 0.0
    last_force_wake = 0.0
    try:
        try:
            vin = await client.resolve_vin()
        except (TeslaAPIError, TeslaAuthError, httpx.HTTPError, OSError) as exc:
            _log(f"cannot resolve VIN: {exc}")
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
        except (TeslaAPIError, TeslaAuthError, httpx.HTTPError, OSError) as exc:
            _log(f"cannot resolve energy site: {exc}")

        while True:
            conf = solar.load_config(store._db)
            st = solar.load_state(store._db, vin)
            solar_wanted = (bool(conf["enabled"]) or bool(st["dirty"])
                            or solar.forcing(conf, time.time()))

            if time.time() - last_site_ingest >= SITE_INGEST_INTERVAL_S:
                last_site_ingest = time.time()
                try:
                    await ingest_site_meters(client, store._db, site_id,
                                             settings, time.time())
                except Exception as exc:      # never let this kill the loop
                    _log(f"site meter ingest error: {exc}")

            # Beat at the TOP, before any branch that can `continue` or
            # `return`. Every early exit below is a legitimate quiet path --
            # cap tripped, recovery pending, auth lost -- and a heartbeat
            # written only on the happy path would report a working collector
            # as dead precisely when something is wrong.
            #
            # THE UNBROKEN WATCH goes with it, and for a closely related
            # reason: both are statements about our own attendance, and both
            # are only honest if they are written on every pass rather than
            # the happy ones. A car seen still asleep one cadence after we
            # last looked has not been driven anywhere, so this is what keeps
            # its snapshot actionable through a night and a morning -- see
            # solar.asleep_confirmed. car_state is the previous pass's
            # reading, which is exactly the claim being extended.
            #
            # The gap tolerance mirrors ha_routes.collector_running: two
            # cadences plus a minute, floored at 300 s. The wider of two
            # cadences, because two different observers are involved.
            # last_sleep_s is the sleep THIS process just completed and is
            # the gap inside a running loop; on the first pass after a
            # restart it is still the 60 s default, while the gap is whatever
            # the previous process was sleeping -- 1800 s after dark -- so
            # its recorded heartbeat_sleep_s has to be allowed to speak for
            # it. Without that, every restart breaks a chain that nothing was
            # actually wrong with. A hole wider than either explains -- a
            # long outage, a suspended Mac -- still breaks it, which is the
            # point.
            now_beat = time.time()
            snap = store.snapshot(vin)
            if car_state == "online":
                # A fresh view is the snapshot's own answer; nothing to carry.
                confirmed = None
            else:
                confirmed = solar.asleep_confirmed(
                    now=now_beat,
                    snapshot_ts=snap["ts"] if snap else None,
                    confirmed_ts=st["asleep_confirmed_ts"],
                    max_gap_s=max(300, 2 * max(
                        last_sleep_s, st["heartbeat_sleep_s"] or 0) + 60))
            solar.save_state(store._db, vin, heartbeat_ts=int(now_beat),
                             heartbeat_sleep_s=int(last_sleep_s),
                             asleep_confirmed_ts=None if confirmed is None
                             else int(confirmed))

            # METER-ONLY WATCH. A plugged-in, hungry car that is asleep
            # needs no vehicle request at all: only the SITE meter can say
            # whether there is anything worth waking for. Skipping the state
            # check halves the cost of a watch tick, which is what makes a
            # tight cadence affordable -- and the car is touched exactly once,
            # when the surplus actually crosses.
            #
            # Gated on the machine being idle or stopped, matching
            # solar_tick's own guard: once engaged we need a real view, and
            # the wake we just issued will supply one on the next pass.
            # A forced charge must WAKE a sleeping car, not watch it: the
            # meter-only watch deliberately makes no vehicle call, and force
            # mode needs one.
            watching = False
            if (car_state != "online" and solar_wanted
                    and not solar.forcing(conf, time.time())
                    and site_id is not None
                    and st["state"] in ("idle", "stopped")):
                watching = solar.sleeping_candidate(
                    snap["view"] if snap else None,
                    solar.knowledge_age_s(now_beat,
                                          snap["ts"] if snap else None,
                                          confirmed),
                    SNAPSHOT_MAX_AGE_S)

            try:
                if watching:
                    # No vehicle call at all this tick.
                    view, ticks_since_view = None, 0
                elif engaged and view is not None:
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

            # A forced charge must WAKE a sleeping car. The meter-only watch
            # is disabled while forcing (above) precisely so we land here --
            # but poll_once returns no view for a sleeping car, and the tick
            # gate below would then skip the tick entirely.
            #
            # Rate-limited AND counted against the cap: this is the most
            # expensive request this system makes, and a car that will not
            # wake must not be asked every tick until midnight.
            if (solar.forcing(conf, time.time()) and view is None
                    and time.time() - last_force_wake >= FORCE_WAKE_MIN_S):
                last_force_wake = time.time()
                today = datetime.now(
                    ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")
                _, capped = solar.count_request(store._db, vin, today)
                if capped:
                    _log("force wake skipped: daily request cap reached")
                else:
                    _log("force charge: waking the car")
                    try:
                        await client.wake_up(vin)
                    except (TeslaAPIError, TeslaAuthError, httpx.HTTPError,
                            OSError) as exc:
                        _log(f"force wake failed: {exc}")
                    else:
                        car_state, view = await poll_once(
                            client, store, vin, settings)

            # Task 17b: independent of solar entirely -- neither gated on
            # solar_wanted nor on recovery, and the scheduled close runs
            # whether or not the car is even reachable this tick (an open
            # garage with the car gone is worse than one with the car in
            # it).
            if view is not None:
                await garage_arrival_tick(store._db, vin, view, conf, home.load(store._db))
            await garage_scheduled_close_tick(store._db, vin, conf, settings.timezone)

            wrote_last_tick = False
            backoff_s = 0
            # `watching` carries no view by design -- solar_tick reads the
            # snapshot itself. Gating solely on `view is not None` here is
            # what kept the watch from ever running.
            if (view is not None or watching) and solar_wanted:
                if not recovery_done:
                    db = store._db
                    recovery_done = await recover(
                        client, db, vin, solar.load_state(db, vin),
                        home.classify(view, home.load(db)), settings, view)
                    if not recovery_done:
                        # Best-effort, not synchronous (spec 3.6): leave dirty
                        # set and retry next tick rather than engaging with a
                        # machine the recovery gate refused to clear.
                        engaged = 0
                        soc = (view or {}).get("soc")
                        _log(f"{car_state}" + (f" soc={soc}%" if soc is not None else "")
                             + " [recovery pending]")
                        if once:
                            return 0
                        await asyncio.sleep(
                            next_interval(car_state, view, settings, 0))
                        continue
                state, wrote_last_tick = await solar_tick(
                    client, store, vin, view, settings, site_id)
                engaged = conf["period_s"] if state in ENGAGED_STATES else 0
                if watching and state not in ("idle", "stopped"):
                    # Engaging from a watch tick necessarily woke the car, but
                    # watch ticks skip poll_once, so car_state is still the
                    # stale "offline" that next_interval short-circuits on --
                    # sending the loop to sleep for poll_asleep instead of
                    # servoing the charge it just started.
                    car_state = "online"

                # Invariant 4 (spec 3.7): a 429 caught during this tick's
                # live_status call lengthens the NEXT sleep instead of
                # retrying at the normal cadence. solar_tick persists the
                # computed backoff into solar_state (so a restart mid-event
                # does not resume hammering) rather than returning it here --
                # every other caller of solar_tick unpacks a plain (state,
                # wrote) pair, and changing that would break them all.
                backoff_s = solar.load_state(store._db, vin)["backoff_s"]
            else:
                engaged = 0

            soc = (view or {}).get("soc")
            _log(f"{car_state}" + (f" soc={soc}%" if soc is not None else ""))
            # Force mode is not waiting for surplus, and must not be stood
            # down after dark -- which is when a forced charge almost always
            # runs.
            waiting_for_surplus = bool(
                solar_wanted
                and not solar.forcing(conf, time.time())
                and (watching
                     or solar.load_state(store._db, vin)["state"]
                     in ("idle", "stopped")))

            # Stand the watch down after dark. Waiting for surplus at 300 s
            # all night spent ~33 ticks a night on this site -- roughly
            # $1.98/month, a fifth of the whole API credit -- asking whether
            # the sun was up at 1 a.m. Falls back to the ordinary asleep/idle
            # cadence, which still notices dawn within half an hour, well
            # before there is 1.2 kW of surplus to act on.
            if waiting_for_surplus and solar.is_dark_at(
                    solar.recent_solar(store._db, vin, solar.DARK_TICKS),
                    time.time()):
                waiting_for_surplus = False

            if once:
                return 0
            last_sleep_s = (
                backoff_s
                or (conf["watch_s"] if waiting_for_surplus
                    else next_interval(car_state, view, settings, engaged)))
            await asyncio.sleep(
                backoff_s
                # Anything WAITING FOR SURPLUS runs at the watch cadence,
                # whether the car is asleep (unreadable, meter-only ticks) or
                # awake and idle. Both are the same situation -- nothing to
                # servo, just a threshold to notice -- and both were falling
                # through to a 1800 s poll: poll_asleep for the sleeping case,
                # poll_idle for the waking one, because "idle" is not in
                # ENGAGED_STATES. Thirty minutes of standing surplus either
                # way, which is the complaint that started this.
                or (conf["watch_s"] if waiting_for_surplus
                    else next_interval(car_state, view, settings, engaged)))
    finally:
        await client.aclose()
        store.close()


def _log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    sys.exit(asyncio.run(run(once=parser.parse_args().once)))
