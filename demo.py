"""Synthetic Fleet API responses, for `DEMO=1 python app.py`.

Deliberately emits the *raw* watt-hour schema Tesla returns, so demo data flows
through exactly the same energy.derive() path as live data — a rendering bug shows
up here rather than after you've finished the Tesla setup.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

SITE_ID = "demo-1"
_rng = random.Random(7)  # fixed seed: the demo looks the same every reload


def _day(dt: datetime, sun: float) -> dict[str, Any]:
    """One day of a ~9 kW array with a Powerwall. `sun` in 0..1 scales production."""
    solar = 38_000 * sun * _rng.uniform(0.92, 1.08)
    home = 31_000 * _rng.uniform(0.85, 1.15)

    home_from_solar = min(solar * 0.42, home * 0.55)
    batt_from_solar = min(solar - home_from_solar, 13_000) * _rng.uniform(0.75, 1.0)
    home_from_batt = min(batt_from_solar * 0.9, home - home_from_solar)
    home_from_grid = max(0.0, home - home_from_solar - home_from_batt)

    solar_to_grid = max(0.0, solar - home_from_solar - batt_from_solar)
    batt_from_grid = 0.0 if sun > 0.5 else 2_000 * _rng.uniform(0, 1)

    return {
        "timestamp": dt.isoformat(),
        "solar_energy_exported": round(solar),
        "generator_energy_exported": 0,
        "grid_energy_imported": round(home_from_grid + batt_from_grid),
        "grid_services_energy_imported": 0,
        "grid_services_energy_exported": 0,
        "grid_energy_exported_from_solar": round(solar_to_grid),
        "grid_energy_exported_from_generator": 0,
        "grid_energy_exported_from_battery": round(batt_from_solar * 0.05),
        "battery_energy_exported": round(home_from_batt),
        "battery_energy_imported_from_grid": round(batt_from_grid),
        "battery_energy_imported_from_solar": round(batt_from_solar),
        "battery_energy_imported_from_generator": 0,
        "consumer_energy_imported_from_grid": round(home_from_grid),
        "consumer_energy_imported_from_solar": round(home_from_solar),
        "consumer_energy_imported_from_battery": round(home_from_batt),
        "consumer_energy_imported_from_generator": 0,
    }


def _integrate_today(tz: str) -> dict[str, Any]:
    """Roll the 5-minute power feed up into one calendar_history energy row (Wh).

    Every step is attributed by where the power actually went, which is the same
    source/destination split Tesla reports — so the house balance holds exactly:
    home = solar->home + grid->home + battery->home.
    """
    now = datetime.now(ZoneInfo(tz))
    acc = {
        "solar": 0.0, "solar_to_home": 0.0, "solar_to_batt": 0.0, "solar_to_grid": 0.0,
        "grid_to_home": 0.0, "batt_to_home": 0.0,
    }

    for row in power_history(tz)["time_series"]:
        solar = row["solar_power"]
        grid = row["grid_power"]        # > 0 import, < 0 export
        battery = row["battery_power"]  # > 0 discharge, < 0 charge

        to_batt = max(0.0, -battery)
        to_grid = max(0.0, -grid)
        acc["solar"] += solar * STEP_H
        acc["solar_to_batt"] += to_batt * STEP_H
        acc["solar_to_grid"] += to_grid * STEP_H
        acc["solar_to_home"] += max(0.0, solar - to_batt - to_grid) * STEP_H
        acc["grid_to_home"] += max(0.0, grid) * STEP_H
        acc["batt_to_home"] += max(0.0, battery) * STEP_H

    r = {k: round(v) for k, v in acc.items()}
    return {
        "timestamp": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
        "solar_energy_exported": r["solar"],
        "generator_energy_exported": 0,
        "grid_energy_imported": r["grid_to_home"],  # nothing charges from grid in this sim
        "grid_services_energy_imported": 0,
        "grid_services_energy_exported": 0,
        "grid_energy_exported_from_solar": r["solar_to_grid"],
        "grid_energy_exported_from_generator": 0,
        "grid_energy_exported_from_battery": 0,
        "battery_energy_exported": r["batt_to_home"],
        "battery_energy_imported_from_grid": 0,
        "battery_energy_imported_from_solar": r["solar_to_batt"],
        "battery_energy_imported_from_generator": 0,
        "consumer_energy_imported_from_grid": r["grid_to_home"],
        "consumer_energy_imported_from_solar": r["solar_to_home"],
        "consumer_energy_imported_from_battery": r["batt_to_home"],
        "consumer_energy_imported_from_generator": 0,
    }


def _seasonal(dt: datetime) -> float:
    """Sun factor: seasonal curve + weather noise."""
    season = 0.62 + 0.38 * math.sin((dt.timetuple().tm_yday - 80) / 365 * 2 * math.pi)
    return max(0.12, min(1.0, season * _rng.uniform(0.55, 1.15)))


def calendar_history(period: str, tz: str) -> dict[str, Any]:
    global _rng
    _rng = random.Random(7)  # reseed per call so totals don't drift on every refresh
    now = datetime.now(ZoneInfo(tz))
    rows: list[dict[str, Any]] = []

    if period == "day":
        # Integrate today's power feed rather than drawing a fresh random day, so the
        # "Today" totals actually agree with the intraday chart sitting beneath them.
        rows = [_integrate_today(tz)]
    elif period == "week":
        start = now - timedelta(days=now.weekday())
        rows = [
            _day(d, _seasonal(d))
            for i in range((now - start).days + 1)
            if (d := (start + timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0))
        ]
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        rows = [_day(start + timedelta(days=i), _seasonal(start + timedelta(days=i)))
                for i in range(now.day)]
    elif period == "year":
        # Monthly buckets: sum 30 synthetic days each so the totals stay plausible.
        for m in range(1, now.month + 1):
            first = now.replace(month=m, day=1, hour=0, minute=0, second=0, microsecond=0)
            days = [_day(first, _seasonal(first)) for _ in range(30)]
            merged = {k: sum(d[k] for d in days) for k in days[0] if k != "timestamp"}
            rows.append({"timestamp": first.isoformat(), **merged})
    elif period == "lifetime":
        for y in range(now.year - 3, now.year + 1):
            first = now.replace(year=y, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            days = [_day(first, _seasonal(first + timedelta(days=i * 30))) for i in range(360)]
            merged = {k: sum(d[k] for d in days) for k in days[0] if k != "timestamp"}
            rows.append({"timestamp": first.isoformat(), **merged})

    return {"time_series": rows}


CAPACITY_WH = 13_500.0
MAX_BATT_W = 5_000.0
STEP_H = 5 / 60  # 5-minute samples


def power_history(tz: str) -> dict[str, Any]:
    """5-minute intraday power samples, midnight -> now.

    The battery follows a real state-of-charge integrator rather than a time-of-day
    rule: it charges until full, then surplus spills to the grid on its own. A hard
    'stop charging at 4pm' cutoff produces a cliff in the export curve that looks
    like a rendering bug.
    """
    rng = random.Random(11)  # local seed: the demo is stable across reloads
    now = datetime.now(ZoneInfo(tz))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    soc = 3_600.0  # Wh in the pack at midnight
    t = start

    while t <= now:
        hour = t.hour + t.minute / 60
        # Solar bell curve, roughly 6am to 8pm, with passing clouds.
        solar = max(0.0, 8_800 * math.sin((hour - 6) / 14 * math.pi)) if 6 < hour < 20 else 0.0
        solar *= rng.uniform(0.82, 1.0)
        load = max(300.0, 1_500 + 900 * math.sin((hour - 7) / 24 * 2 * math.pi) + rng.uniform(-260, 620))

        surplus = solar - load
        if surplus > 0:
            headroom_w = (CAPACITY_WH - soc) / STEP_H  # what the pack can absorb this step
            charge = min(surplus, MAX_BATT_W, max(0.0, headroom_w))
            battery = -charge                    # negative = charging
            grid = -(surplus - charge)           # negative = exporting the rest
            soc += charge * STEP_H
        else:
            deficit = -surplus
            available_w = soc / STEP_H
            discharge = min(deficit, MAX_BATT_W, max(0.0, available_w))
            battery = discharge                  # positive = discharging
            grid = deficit - discharge           # positive = importing the rest
            soc -= discharge * STEP_H

        rows.append({
            "timestamp": t.isoformat(),
            "solar_power": round(solar),
            "battery_power": round(battery),
            "grid_power": round(grid),
            "generator_power": 0,
            "grid_services_power": 0,
            "_soc": soc,
        })
        t += timedelta(minutes=5)
    return {"time_series": rows}


def live_status(tz: str) -> dict[str, Any]:
    latest = power_history(tz)["time_series"][-1]
    return {
        "percentage_charged": round(latest["_soc"] / CAPACITY_WH * 100, 1),
        "energy_left": round(latest["_soc"]),
        "solar_power": latest["solar_power"],
        "grid_power": latest["grid_power"],
        "battery_power": latest["battery_power"],
        # Tesla's site balance: load = solar + grid + battery, where grid > 0 is import
        # and battery > 0 is discharge. Getting this sign wrong makes the house appear
        # to consume power it is simultaneously exporting.
        "load_power": latest["solar_power"] + latest["grid_power"] + latest["battery_power"],
        "total_pack_energy": round(CAPACITY_WH),
        "grid_status": "Active",
        "storm_mode_active": False,
        "island_status": "on_grid",
        "timestamp": datetime.now(ZoneInfo(tz)).isoformat(),
    }


def sites() -> list[dict[str, Any]]:
    return [{
        "energy_site_id": SITE_ID,
        "site_name": "Demo Solar + Powerwall",
        "resource_type": "battery",
        "components": {"solar": True, "battery": True},
    }]


def site_info() -> dict[str, Any]:
    return {
        "site_name": "Demo Solar + Powerwall",
        "components": {"solar": True, "battery": True},
        "solar_power": 9_000,
    }
