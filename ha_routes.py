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
        "SELECT ts, surplus_w, amps_written, amps_before FROM solar_ticks"
        " WHERE vin = ? ORDER BY ts DESC LIMIT 1", (vin,)).fetchone() if vin else None

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

        # Tunables HA may display and (later) write.
        "margin_w": conf["margin_w"],
        "min_a": conf["min_a"],
        "soc_ceiling": conf["soc_ceiling"],
        "grace_budget_wh": conf["grace_budget_wh"],
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
    if not vin:
        # null, never 0. HA discards non-numeric states from statistics, so
        # unavailable is harmless -- while a 0 would read as a counter reset
        # and inject the entire lifetime total into one 5-minute bucket.
        return {"schema": SCHEMA, "car_solar_kwh": None, "car_grid_kwh": None,
                "car_total_kwh": None}

    ticks = [dict(r) for r in db.execute(
        "SELECT state, car_w, grid_w, period_s FROM solar_ticks WHERE vin = ?",
        (vin,))]
    solar_kwh, grid_kwh = green.charged_split(ticks)
    return {
        "schema": SCHEMA,
        "car_solar_kwh": round(solar_kwh, 3),
        "car_grid_kwh": round(grid_kwh, 3),
        "car_total_kwh": round(solar_kwh + grid_kwh, 3),
    }
