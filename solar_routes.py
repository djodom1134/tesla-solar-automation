"""HTTP surface for home configuration and the solar controller.

This module never commands the car. The collector owns every write to the
vehicle; the web app only reads status and edits configuration, which the
collector picks up on its next tick. That keeps exactly one process issuing
commands and makes the signing proxy's per-VIN mutex a non-issue.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, HTTPException

import home
import solar
from config import settings
from store import Store

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


@router.get("/home")
async def get_home() -> dict[str, Any]:
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
        "engaged_at": state["engaged_at"],
        "last_tick_ts": last["ts"] if last else None,
        "requests_today": state["requests_today"] if state["requests_day"] == _today() else 0,
    }
