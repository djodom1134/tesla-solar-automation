"""HTTP surface for home configuration and the solar controller.

This module never commands the car. The collector owns every write to the
vehicle; the web app only reads status and edits configuration, which the
collector picks up on its next tick. That keeps exactly one process issuing
commands and makes the signing proxy's per-VIN mutex a non-issue.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, HTTPException

import demo
import green
import home
import solar
from config import settings
from store import Store

DEMO = os.getenv("DEMO", "").strip() in {"1", "true", "yes"}

router = APIRouter(prefix="/api/car")

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(settings.db_file)
    return _store


def _vin() -> str:
    """The VIN the collector is recording under.

    Prefers the configured one; otherwise the most recent sample. Returns ""
    when nothing has ever been recorded, which every caller tolerates.
    """
    if settings.vin:
        return settings.vin
    row = store()._db.execute(
        "SELECT vin FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    return row["vin"] if row else ""


def _today() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")


def _midnight_ts() -> int:
    now = datetime.now(ZoneInfo(settings.timezone))
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _solar_ticks(db, vin: str, since_ts: int) -> list[dict[str, Any]]:
    """Rows for green.solar_kwh(). Same since_ts convention as
    solar.grace_import_wh() -- 0 for all time, _midnight_ts() for today."""
    if not vin:
        return []
    rows = db.execute(
        "SELECT state, car_w, grid_w, period_s FROM solar_ticks"
        " WHERE vin = ? AND ts >= ?", (vin, since_ts),
    ).fetchall()
    return [dict(row) for row in rows]


def _sessions_and_segments(
    db, vin: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Charge sessions and driving segments, both derived from the same
    ordered sample history and both bounded by the same charging-run edges.

    A charging run's own first and last row mark the exact moment charging
    started or stopped, so that row doubles as the boundary anchor for the
    *adjacent* driving segment too. Anchoring a segment at the next-available
    sample instead -- rather than at the charging run's edge -- would
    silently drop whatever the car did in between: often hours and several
    miles, since the collector polls slowly once it has no reason to hurry
    (see collector.next_interval). Using the charging edges as shared anchors
    means every mile and every percent of SoC in the whole history lands in
    exactly one bucket: a charge session or a driving segment, never neither.
    """
    if not vin:
        return [], []
    rows = db.execute(
        "SELECT battery_level, charging, charge_energy_added, odometer"
        " FROM samples WHERE vin = ? ORDER BY ts", (vin,),
    ).fetchall()
    n = len(rows)
    if n == 0:
        return [], []

    charging_runs: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if rows[i]["charging"]:
            j = i
            while j < n and rows[j]["charging"]:
                j += 1
            charging_runs.append((i, j - 1))
            i = j
        else:
            i += 1

    sessions = []
    for start, end in charging_runs:
        first, last = rows[start], rows[end]
        added_start, added_end = first["charge_energy_added"], last["charge_energy_added"]
        if added_start is None or added_end is None:
            continue  # charge_energy_added missing for this run -- not usable
        sessions.append({
            "soc_start": first["battery_level"], "soc_end": last["battery_level"],
            "kwh_added": added_end - added_start,
        })

    anchors = [0]
    for start, end in charging_runs:
        anchors += [start, end]
    anchors.append(n - 1)

    segments = []
    for k in range(0, len(anchors) - 1, 2):
        start_idx, end_idx = anchors[k], anchors[k + 1]
        if start_idx == end_idx:
            continue
        first, last = rows[start_idx], rows[end_idx]
        if first["odometer"] is None or last["odometer"] is None:
            continue
        if first["battery_level"] is None or last["battery_level"] is None:
            continue
        segments.append({
            "miles": last["odometer"] - first["odometer"],
            "soc_drop": first["battery_level"] - last["battery_level"],
        })

    return sessions, segments


def _green_status(db, vin: str) -> dict[str, Any]:
    """The three-number "free miles" answer, honest about what it doesn't
    know yet. mi_per_kwh and pack_kwh are lifetime figures -- they only get
    more trustworthy with more history, so there is no reason to reset them
    daily. solar_kwh_today is the one number scoped to the calendar day, same
    convention as grace_import_wh_today: it is "free miles earned today",
    not a lifetime tally, so it goes back to zero each morning even though
    the pack and the odometer never do.
    """
    solar_today = green.solar_kwh(_solar_ticks(db, vin, _midnight_ts()))
    sessions, segments = _sessions_and_segments(db, vin)
    pack, pack_n = green.pack_kwh(sessions)
    mpk, miles = green.miles_per_kwh(segments, pack)
    free = green.free_miles(solar_today, mpk)
    return {
        "free_miles": round(free, 1) if free is not None else None,
        "solar_kwh_today": round(solar_today, 2),
        "mi_per_kwh": round(mpk, 2) if mpk is not None else None,
        "miles_sampled": round(miles, 1),
        "pack_kwh": round(pack, 1) if pack is not None else None,
        "pack_sessions": pack_n,
    }


@router.get("/home")
async def get_home() -> dict[str, Any]:
    if DEMO:
        return demo.home_config()

    db = store()._db
    cfg = home.load(db)
    vin = _vin()
    snap = store().snapshot(vin) if vin else None
    view = (snap or {}).get("view") or {}
    car = None
    if view.get("lat") is not None and view.get("lon") is not None:
        car = {"lat": view["lat"], "lon": view["lon"]}
    return {
        "home": None if cfg is None else {
            "latitude": cfg.latitude, "longitude": cfg.longitude,
            "radius_m": cfg.radius_m,
        },
        "classification": home.classify(view, cfg),
        "car": car,
    }


@router.put("/home")
async def put_home(body: dict[str, Any] = Body(...)) -> dict[str, bool]:
    try:
        lat = float(body["latitude"])
        lon = float(body["longitude"])
        radius = int(body["radius_m"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "latitude, longitude and radius_m are required")
    if not -90 <= lat <= 90:
        raise HTTPException(400, "latitude must be between -90 and 90")
    if not -180 <= lon <= 180:
        raise HTTPException(400, "longitude must be between -180 and 180")
    if not 10 <= radius <= 2000:
        raise HTTPException(400, "radius_m must be between 10 and 2000")
    home.save(store()._db, lat, lon, radius)
    return {"ok": True}


# Bounds are validation, not taste. period_s has a hard floor because
# grid_power only refreshes every 60 s; anything faster re-reads one value.
CONFIG_BOUNDS = {
    "enabled": (0, 1), "period_s": (60, 900), "margin_w": (0, 2000),
    "deadband_w": (50, 2000), "ramp_a": (1, 48), "min_a": (5, 32),
    "grace_s": (0, 3600), "restart_hold_s": (60, 3600),
    "start_hold_s": (0, 3600), "raise_hold_s": (0, 7200),
    "soc_ceiling": (50, 100), "raise_limit": (0, 1),
    "daily_request_cap": (0, 20000), "view_refresh_ticks": (1, 60),
    "deadline_soc": (0, 100),
    "deadline_hour": (0, 23),
}


@router.get("/solar/config")
async def get_solar_config() -> dict[str, Any]:
    return solar.load_config(store()._db)


@router.put("/solar/config")
async def put_solar_config(body: dict[str, Any] = Body(...)) -> dict[str, bool]:
    unknown = set(body) - set(CONFIG_BOUNDS)
    if unknown:
        raise HTTPException(400, f"unknown fields: {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for key, value in body.items():
        if value is None and key in ("deadline_soc", "deadline_hour"):
            clean[key] = None
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} must be an integer")
        low, high = CONFIG_BOUNDS[key]
        if not low <= number <= high:
            raise HTTPException(400, f"{key} must be between {low} and {high}")
        clean[key] = number
    solar.save_config(store()._db, **clean)
    return {"ok": True}


@router.get("/solar/status")
async def get_solar_status() -> dict[str, Any]:
    if DEMO:
        return demo.solar_status()

    db = store()._db
    vin = _vin()
    state = solar.load_state(db, vin) if vin else dict(solar.STATE_DEFAULTS)
    snap = store().snapshot(vin) if vin else None
    view = (snap or {}).get("view") or {}
    last = db.execute(
        "SELECT ts, surplus_w, amps_written, amps_before FROM solar_ticks"
        " WHERE vin = ? ORDER BY ts DESC LIMIT 1", (vin,)).fetchone() if vin else None
    return {
        "state": state["state"],
        "enabled": bool(solar.load_config(db)["enabled"]),
        "surplus_w": last["surplus_w"] if last else None,
        "amps": (last["amps_written"] or last["amps_before"]) if last else None,
        "soc": view.get("soc"),
        "limit": view.get("limit"),
        "raised_to": state["raised_to"],
        "original_limit": state["original_limit"],
        "grace_import_wh_today": round(
            solar.grace_import_wh(db, vin, _midnight_ts()), 1) if vin else 0,
        "grace_import_wh_total": round(
            solar.grace_import_wh(db, vin, 0), 1) if vin else 0,
        "capped": bool(state["capped"]),
        "dirty": bool(state["dirty"]),
        # Invariant 4 (spec 3.7): a sustained 429 means the controller is
        # polling slower than the owner's configured period thinks -- surface
        # it the same way capped/dirty are, not just in the collector's log.
        "rate_limited": bool(state["consecutive_429s"]),
        "backoff_s": state["backoff_s"],
        "engaged_at": state["engaged_at"],
        "last_tick_ts": last["ts"] if last else None,
        "requests_today": state["requests_today"] if state["requests_day"] == _today() else 0,
        **_green_status(db, vin),
    }
