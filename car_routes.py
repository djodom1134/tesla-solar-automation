"""HTTP surface for the car page.

Reads go direct to the Fleet API; commands (Task 11) go through the signing
proxy. Everything here tolerates a sleeping car, because that is its normal
state — a request that cannot reach the vehicle falls back to the last stored
snapshot with its age, rather than failing."""
from __future__ import annotations

import asyncio
import os
import socket
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query

import demo
import vehicle
from config import settings
from store import Store
from tesla import TeslaAPIError, TeslaAuthError, TeslaClient, VehicleAsleep

DEMO = os.getenv("DEMO", "").strip() in {"1", "true", "yes"}

RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400,
          "90d": 90 * 86400, "all": 0}

REQUIRED_SCOPES = ["vehicle_device_data", "vehicle_location",
                   "vehicle_cmds", "vehicle_charging_cmds"]

router = APIRouter(prefix="/api/car")

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(settings.db_file)
    return _store


def _client() -> TeslaClient:
    from app import client
    return client


async def _vin() -> str:
    return "5YJSA00000F000000" if DEMO else await _client().resolve_vin()


@router.get("/state")
async def car_state() -> dict[str, Any]:
    """Live data when the car is awake, the stored snapshot with its age when not."""
    vin = await _vin()

    if DEMO:
        view = vehicle.derive(demo.vehicle_data(settings.timezone))
        return {"vin": vin, "view": view, "age_seconds": 0,
                "car_state": "online", "live": True, "source": "live"}

    client = _client()
    try:
        car = (await client.vehicle(vin)).get("state") or "offline"
    except TeslaAPIError:
        car = "offline"

    if car == "online":
        try:
            view = vehicle.derive(await client.vehicle_data(vin))
            store().record(view)  # a page view contributes history for free
            return {"vin": vin, "view": view, "age_seconds": 0,
                    "car_state": car, "live": True, "source": "live"}
        except (VehicleAsleep, TeslaAPIError):
            car = "asleep"

    snap = store().snapshot(vin)
    if snap is None:
        return {"vin": vin, "view": None, "age_seconds": None,
                "car_state": car, "live": False, "source": "none"}
    return {"vin": vin, "view": snap["view"],
            "age_seconds": max(0, int(time.time()) - snap["ts"]),
            "car_state": car, "live": False, "source": "snapshot"}


@router.get("/history")
async def car_history(range: str = Query("24h")) -> dict[str, Any]:
    if range not in RANGES:
        raise HTTPException(400, f"range must be one of {sorted(RANGES)}")
    vin = await _vin()

    if DEMO:
        days = {"24h": 1, "7d": 7, "30d": 30, "90d": 90, "all": 90}[range]
        rows = demo.soc_history(days, settings.timezone)
        return {"rows": rows, "since": rows[0]["ts"] if rows else None,
                "range": range}

    now = int(time.time())
    first = store().first_sample(vin)
    if range == "all":
        # Bucket across the real data span, not an arbitrary window, or a sparse
        # history collapses into a single point.
        start = first if first is not None else now
    else:
        start = now - RANGES[range]
    rows = store().history(vin, start, now + 1)
    return {"rows": rows, "since": first, "range": range}


@router.post("/wake")
async def car_wake() -> dict[str, Any]:
    """Explicit user action only. Never called on a timer or a page load."""
    if DEMO:
        return {"state": "online"}
    vin = await _vin()
    result = await _client().wake_up(vin)
    return {"state": (result or {}).get("state", "unknown")}


@router.get("/health")
async def car_health() -> dict[str, Any]:
    vin = await _vin()
    scopes: list[str] = []
    key_paired: bool | None = None
    # Distinguishes "not logged in" from "logged in but the token didn't
    # decode" from "no problem" (None). Both failure modes used to collapse
    # into scopes: [] with no way for an operator to tell them apart -- and
    # because missing_scopes short-circuits on falsy scopes, into an
    # identical missing_scopes: [] too.
    auth_error: str | None = None

    if not DEMO:
        import base64
        import json
        try:
            token = await _client()._access_token()
        except TeslaAuthError:
            auth_error = "not_authenticated"
        else:
            try:
                payload = token.split(".")[1]
                payload += "=" * (-len(payload) % 4)
                scopes = json.loads(base64.urlsafe_b64decode(payload)).get("scp", [])
            except Exception:
                auth_error = "scopes_decode_failed"
        try:
            status = await _client().fleet_status([vin])
            key_paired = vin in (status or {}).get("key_paired_vins", [])
        except TeslaAPIError:
            key_paired = None

    last = store().snapshot(vin)
    day_ago = int(time.time()) - 86400
    return {
        "scopes": scopes,
        "missing_scopes": [s for s in REQUIRED_SCOPES if scopes and s not in scopes],
        "key_paired": True if DEMO else key_paired,
        "auth_error": auth_error,
        # Off the event loop: this is a blocking socket call with a 0.5s
        # timeout, hit on every page load and every 60s health poll. Run
        # synchronously it would stall the whole single-threaded app for up
        # to half a second per call.
        "proxy": True if DEMO else await asyncio.to_thread(_proxy_up),
        "collector": {
            "running": bool(last and int(time.time()) - last["ts"] < 3600),
            "last_sample": last["ts"] if last else None,
        },
        "calls_today": store().count_since(day_ago),
    }


def _proxy_up() -> bool:
    """TCP reachability only — cheap, and enough to tell the UI whether to
    enable the controls."""
    parsed = urlparse(settings.proxy_url)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 443), timeout=0.5
        ):
            return True
    except OSError:
        return False
