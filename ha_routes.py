"""Home Assistant's view of this service.

HA POLLS US. That direction is not an accident:

  * macOS 15 gates OUTBOUND connections to LAN addresses per application, and
    a launchd agent cannot raise the approval prompt -- which is why the
    garage is currently unreachable from our own collector. Anything where we
    dial out (an MQTT client, pushing to HA's REST API) is blocked today.
    HA connecting to our listening socket is inbound, and is not gated.
  * HA's mqtt integration declares single_config_entry: True -- it accepts
    exactly ONE broker, forever. Spending that slot here would permanently
    foreclose consuming the ALPR topics already published on servy.

THIS MODULE MUST NEVER IMPORT THE TESLA CLIENT. That is the whole cost
guarantee, and it is enforced by a test rather than by good intentions: an HA
dashboard left open on a wall tablet polls every 60 s forever, and if any of
that reached the Fleet API it would quietly drain a $10/month credit. Every
value here comes from SQLite, which the collector fills on its own schedule.

`schema` is returned so HA can tell data from an error body. The rest
platform never calls raise_for_status(), so a 401 or a 500 arrives as a
perfectly parseable JSON dict; an availability template that merely checks
for a key would happily treat {"detail": "..."} as live data.
"""
from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter

import green
import home
import meters
import solar
from config import settings
from store import Store

DEMO = os.getenv("DEMO", "").strip() in {"1", "true", "yes"}

SCHEMA = 1

router = APIRouter(prefix="/api/ha")

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(settings.db_file)
    return _store


def _vin() -> str:
    """The VIN, from SQLite only -- never resolve_vin(), which is billed."""
    if settings.vin:
        return settings.vin
    return store().latest_vin() or ""


def collector_running(st: dict, now: float) -> bool:
    """Whether the collector has beaten recently enough to be believed.

    Judged against the cadence ACTUALLY IN FORCE, not a constant. The loop
    sleeps 120 s while engaged and 1800 s after dark, so a fixed threshold is
    either useless by day or cries wolf every night. Two missed beats plus a
    minute of slack, floored at 300 s so a fast cadence cannot make the
    window absurdly tight.

    Unknown means False. A collector that has never written a heartbeat is
    one we cannot vouch for, and this project's rule is that a stale number
    presented as current is worse than no number.
    """
    beat = st.get("heartbeat_ts")
    if not beat:
        return False
    slept = st.get("heartbeat_sleep_s") or 60
    return (now - beat) < max(300, 2 * slept + 60)


@router.get("/state")
async def ha_state() -> dict[str, Any]:
    """Everything HA needs, from the database, in one request."""
    if DEMO:
        return {"schema": SCHEMA, "demo": True, "collector_running": False}

    db = store()._db
    vin = _vin()
    now = time.time()

    st = solar.load_state(db, vin) if vin else dict(solar.STATE_DEFAULTS)
    conf = solar.load_config(db)
    snap = store().snapshot(vin) if vin else None
    view = (snap or {}).get("view") or {}

    last = db.execute(
        "SELECT ts, surplus_w, amps_written, amps_before, solar_w, grid_w, car_w"
        " FROM solar_ticks WHERE vin = ? ORDER BY ts DESC LIMIT 1",
        (vin,)).fetchone() if vin else None

    # LIVE POWER, from the tick log rather than a fresh live_status call.
    # live_status is a billed request; the collector already pays for one
    # every tick and writes the result here, so serving it again costs
    # nothing. The price is freshness -- as recent as the last tick, which is
    # 120 s while engaged and up to 1800 s after dark -- and the HA templates
    # gate on exactly that rather than presenting a stale number as live.
    solar_w = last["solar_w"] if last else None
    grid_w = last["grid_w"] if last else None
    car_w = last["car_w"] if last else None
    # The house alone: everything the site drew minus what the car took.
    # solar + grid is total site consumption (grid positive = import).
    house_w = (solar_w + grid_w - (car_w or 0)
               if solar_w is not None and grid_w is not None else None)

    # Three separate clocks, never conflated. Each gates a different set of
    # entities, because "the collector is alive", "the control loop ran
    # recently" and "we have seen the car recently" fail independently.
    return {
        "schema": SCHEMA,
        "collector_running": collector_running(st, now),
        "heartbeat_age_s": int(now - st["heartbeat_ts"]) if st["heartbeat_ts"] else None,
        "last_tick_ts": last["ts"] if last else None,
        "snapshot_age_s": int(now - snap["ts"]) if snap else None,

        "state": st["state"],
        "enabled": bool(conf["enabled"]),
        "raise_limit": bool(conf["raise_limit"]),
        "surplus_w": last["surplus_w"] if last else None,
        "amps": (last["amps_written"] or last["amps_before"]) if last else None,

        # Flags an automation can act on.
        "capped": bool(st["capped"]),
        "dirty": bool(st["dirty"]),
        "rate_limited": bool(st["backoff_s"]),
        "ledger_stale": bool(st["ledger_stale"]),
        "requests_today": st["requests_today"],
        "daily_request_cap": conf["daily_request_cap"],

        # The car, from the stored snapshot. Carrying these here is what stops
        # anyone pointing HA at /api/car/state, which IS billed.
        "soc": view.get("soc"),
        "limit": view.get("limit"),
        "range_mi": view.get("range_mi"),
        "odometer_mi": view.get("odometer_mi"),
        "plugged_in": view.get("charging_state") not in (None, "Disconnected"),
        "charging": view.get("charging_state") in solar.LIVE_CHARGING_STATES,
        "charging_state": view.get("charging_state"),
        # Three-valued on purpose: Tesla OMITS location keys rather than
        # nulling them, so "scope revoked", "sharing off" and "genuinely
        # elsewhere" are indistinguishable. HA maps unknown to unavailable
        # rather than to not_home, or every "car left" automation fires each
        # time the car falls asleep in the garage.
        "location": home.classify(view, home.load(db)) if view else "unknown",

        # Tunables HA may display and write.
        "margin_w": conf["margin_w"],
        "min_a": conf["min_a"],
        "soc_ceiling": conf["soc_ceiling"],
        "grace_budget_wh": conf["grace_budget_wh"],

        # --- live power, all from the last tick (free) --------------------
        "solar_w": solar_w,
        # Signed, Tesla's own convention: positive is IMPORT.
        "grid_w": grid_w,
        # And split, because HA graphs a non-negative series far better than
        # one that crosses zero.
        "grid_import_w": max(0.0, grid_w) if grid_w is not None else None,
        "grid_export_w": max(0.0, -grid_w) if grid_w is not None else None,
        "car_w": car_w,
        "house_w": house_w,

        # --- everything else Tesla told us, verbatim ----------------------
        # The whole derived view. It costs nothing extra (one snapshot read
        # already happened) and means a new field needs no endpoint change --
        # only a template. Nested dicts (doors, windows, tpms_bar, ...) come
        # through intact.
        "car": view,
    }


@router.get("/meters")
async def ha_meters() -> dict[str, Any]:
    """Cumulative energy, for the Energy Dashboard.

    kWh and monotonic, because HA requires state_class total/total_increasing
    for an energy source -- instantaneous power is not eligible. These are
    derived from the never-pruned tick log, so they only ever grow.

    DEMO returns 503 rather than numbers: demo.solar_status() hardcodes
    plausible-looking totals, and feeding those to HA's long-term statistics
    would poison them permanently.
    """
    if DEMO:
        from fastapi import HTTPException
        raise HTTPException(503, "demo mode serves no meters")

    db = store()._db
    vin = _vin()
    # Site channels come from the ratchet, and are independent of the car:
    # they are still true when no car has ever been recorded.
    site = {f"{ch}_kwh": meters.kwh(db, ch) for ch in meters.CHANNELS}

    if not vin:
        # null, never 0. HA discards non-numeric states from statistics, so
        # unavailable is harmless -- while a 0 would read as a counter reset
        # and inject the entire lifetime total into one 5-minute bucket.
        return {"schema": SCHEMA, "car_solar_kwh": None, "car_grid_kwh": None,
                "car_total_kwh": None, **site}

    ticks = [dict(r) for r in db.execute(
        "SELECT state, car_w, grid_w, period_s FROM solar_ticks WHERE vin = ?",
        (vin,))]
    solar_kwh, grid_kwh = green.charged_split(ticks)
    total = solar_kwh + grid_kwh

    return {
        "schema": SCHEMA,
        "car_solar_kwh": round(solar_kwh, 3),
        "car_grid_kwh": round(grid_kwh, 3),
        "car_total_kwh": round(total, 3),

        # --- the car AS STORAGE, for the Energy Dashboard battery slots ----
        #
        # HA computes:
        #   home = solar + grid_import - grid_export + battery_out - battery_in
        #
        # battery_IN is genuinely right here. Energy charged into the car is
        # not consumed by the house -- it is stored in something that then
        # drives away -- so subtracting it makes "home consumption" mean the
        # house WITHOUT the car, which is the more useful number.
        #
        # battery_OUT is permanently ZERO, and that is not a placeholder. This
        # site has no vehicle-to-home: the car never returns energy to the
        # house. Feeding driving energy here would ADD it to home consumption
        # and inflate the house load by every mile driven -- energy that left
        # the property entirely.
        #
        # A counter pinned at 0.0 is safe for total_increasing: it never
        # decreases, so it can never trip the meter-reset rule.
        "battery_in_kwh": round(total, 3),
        "battery_out_kwh": 0.0,
        **site,
    }
