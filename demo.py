"""Synthetic Fleet API responses, for `DEMO=1 python app.py`.

Deliberately emits the *raw* watt-hour schema Tesla returns, so demo data flows
through exactly the same energy.derive() path as live data — a rendering bug shows
up here rather than after you've finished the Tesla setup.
"""
from __future__ import annotations

import math
import random
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import store

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


# ---------------------------------------------------------------- vehicle

def vehicle_data(tz: str) -> dict:
    """Synthetic vehicle_data shaped exactly like Tesla's, so it flows through
    the real vehicle.derive path. The car page cannot be built against a real
    car that is asleep most of the time."""
    now = datetime.now(ZoneInfo(tz))
    charging = 1 <= now.hour < 5
    soc = 62 + (now.hour % 7)
    return {
        "vin": "5YJSA00000F000000",
        "state": "online",
        "charge_state": {
            "battery_level": soc,
            "usable_battery_level": soc - 2,
            "charge_limit_soc": 80,
            "charge_limit_soc_min": 50,
            "charge_limit_soc_max": 100,
            "charging_state": "Charging" if charging else "Disconnected",
            "charger_power": 11 if charging else 0,
            "charger_voltage": 240 if charging else 2,
            "charger_actual_current": 48 if charging else 0,
            "charge_current_request": 48,
            "charge_current_request_max": 48,
            "minutes_to_full_charge": 95 if charging else 0,
            "charge_energy_added": 14.2 if charging else 0.0,
            "battery_range": soc * 3.4,
            "est_battery_range": soc * 3.1,
            "conn_charge_cable": "IEC" if charging else "<invalid>",
            "charge_port_door_open": charging,
            "charge_port_latch": "Engaged" if charging else "Disengaged",
            "charge_port_color": "FlashingGreen" if charging else "Off",
            "fast_charger_type": "<invalid>",
            "scheduled_charging_mode": "Off",
        },
        "climate_state": {
            "inside_temp": 21.5, "outside_temp": 14.0,
            "driver_temp_setting": 21.0, "passenger_temp_setting": 21.0,
            "min_avail_temp": 15.0, "max_avail_temp": 28.0,
            "is_climate_on": False, "is_auto_conditioning_on": False,
            "is_preconditioning": False, "climate_keeper_mode": "off",
            "defrost_mode": 0,
            "seat_heater_left": 0, "seat_heater_right": 0,
            "steering_wheel_heater": False,
            "remote_heater_control_enabled": True,
        },
        "drive_state": {
            # Denver, so the map has somewhere to point.
            "latitude": 39.7392, "longitude": -104.9903, "heading": 215,
            "speed": None, "shift_state": None, "power": 0,
            "gps_as_of": int(now.timestamp()),
            "timestamp": int(now.timestamp() * 1000),
        },
        "vehicle_state": {
            "vehicle_name": "Stallion (demo)",
            "odometer": 24680.5,
            "locked": True,
            "df": 0, "dr": 0, "pf": 0, "pr": 0, "ft": 0, "rt": 0,
            "fd_window": 0, "fp_window": 0, "rd_window": 0, "rp_window": 0,
            "sentry_mode": True, "sentry_mode_available": True,
            "dashcam_state": "Recording",
            "is_user_present": False, "valet_mode": False,
            "car_version": "2026.14.3 abcdef0",
            "tpms_pressure_fl": 3.1, "tpms_pressure_fr": 3.1,
            "tpms_pressure_rl": 3.0, "tpms_pressure_rr": 2.6,
            "tpms_soft_warning_rr": True,
            "software_update": {"status": "", "version": " ",
                                "download_perc": 0, "install_perc": 1},
        },
        "vehicle_config": {"rear_seat_heaters": 1, "has_seat_cooling": False,
                           "sun_roof_installed": 0},
        "gui_settings": {"gui_distance_units": "mi/hr"},
    }


def soc_history(days: int, tz: str) -> list[dict]:
    """A week of plausible SoC: overnight charges, daily drives, and — the point
    of this fixture — sleep gaps the chart has to render as dashed."""
    zone = ZoneInfo(tz)
    now = datetime.now(zone)
    rows: list[dict] = []
    soc = 70
    start = now - timedelta(days=days)
    step = 300
    t = start
    while t < now:
        hour = t.hour
        asleep = 5 <= hour < 7 or 10 <= hour < 15
        if not asleep:
            if 1 <= hour < 5 and soc < 80:
                soc = min(80, soc + 1)
                charging = True
            else:
                charging = False
                if 7 <= hour < 9 or 17 <= hour < 19:
                    soc = max(12, soc - 1)
            rows.append({
                "ts": int(t.timestamp()),
                "soc": soc,
                "usable_soc": soc - 2,
                "charging": charging,
                "gap": False,
            })
        t += timedelta(seconds=step)

    # Mark the first sample after each hole, exactly as store.history does.
    # Reads store.GAP_SECONDS live (module attribute access, not a bound
    # local) so this can never silently drift from the store's real
    # definition of what counts as a sleep gap.
    for i in range(1, len(rows)):
        rows[i]["gap"] = rows[i]["ts"] - rows[i - 1]["ts"] > store.GAP_SECONDS
    return rows


# ---------------------------------------------------------------- solar controller

def solar_status() -> dict:
    """A mid-session solar charge, so the card can be built without a car."""
    return {
        "state": "charging",
        "enabled": True,
        "surplus_w": 6240.0,
        # 6.24 kW of sun at ~3.4 mi/kWh -- fast enough that the
        # thousandths digit visibly moves, which is the point.
        "accrual_mi_per_s": round(6.24 * 3.4 / 3600, 8),
        "accrual_basis": "measured",
        "as_of": 1785250000,
        "amps": 26,
        "soc": 72,
        "limit": 90,
        "raised_to": 90,
        "original_limit": 80,
        "grace_import_wh_today": 41.3,
        "grace_import_wh_total": 512.8,
        "capped": False,
        "dirty": False,
        "rate_limited": False,
        "backoff_s": 0,
        "engaged_at": int(time.time()) - 4200,
        "last_tick_ts": int(time.time()) - 40,
        "requests_today": 173,
        # A demo car with months of history behind it, so the card can show
        # what the fully-resolved feature looks like -- a real, fresh
        # install reports every one of these as None (see solar_routes.py).
        "free_miles": 44.4,
        "solar_kwh_today": 12.0,
        "mi_per_kwh": 3.7,
        "miles_sampled": 812.4,
        "pack_kwh": 81.1,
        "pack_sessions": 6,
        # Task 18: the banked-solar ledger -- a stock, not a flow, so unlike
        # solar_kwh_today above it does not reset at midnight. 9.4% of the
        # demo car's current 72% charge is sun that never left the pack.
        "ledger_stale": False,
        "banked_pct": 9.4,
        "banked_miles_rated": 31.5,
        "banked_miles_measured": 28.2,
        "banked_miles": 28.2,
        "banked_miles_basis": "measured",
        # Lifetime energy INTO the car, split by where it came from. The
        # demo car is mostly-but-not-entirely solar, which is the honest
        # shape: a controller that holds the floor through clouds imports a
        # little on purpose, and pretending otherwise is what this split
        # exists to stop.
        "charged_solar_kwh": 214.6,
        "charged_grid_kwh": 38.9,
        "charged_solar_miles": 794.0,
        "charged_grid_miles": 143.9,
        "charged_solar_share": 84.7,
        # "measured" only -- the demo car has months of history, so mi/kWh is
        # real. A fresh install reports None here and the card shows kWh
        # alone, because a miles figure resting on an assumed pack size
        # contradicts banked_miles, which needs no pack size at all.
        "charged_miles_basis": "measured",
        # Task 20: lifetime free miles driven -- a demo car with months of
        # history behind it, so the promoted headline has something to
        # show. 128.4 / 431.7 = 29.7%, the brief's own worked example.
        "free_miles_driven": 128.4,
        "tracked_miles": 431.7,
        "free_miles_share": 29.7,
        "free_miles_since": int(time.time()) - 86400 * 21,
    }


def garage_status() -> dict:
    """A reachable door, closed and clear, so the car page's garage card can
    be built and dogfooded without real ratgdo hardware."""
    return {"reachable": True, "door_state": "Closed", "obstructed": False,
            "light_on": False}


def home_config() -> dict:
    return {
        "home": {"latitude": 40.1672, "longitude": -105.1019, "radius_m": 100},
        "classification": "home",
        "car": {"lat": 40.1673, "lon": -105.1018},
    }


def landmarks() -> dict:
    """Enough places either side of the threshold that the reveal is visible."""
    return {
        "banked_miles": 24.0,
        "round_trip": True,
        "places": [
            {"name": "Roosevelt Park", "miles": 4.2, "needed": 8.4,
             "mountain": False, "reachable": True},
            {"name": "Union Reservoir", "miles": 7.6, "needed": 15.2,
             "mountain": False, "reachable": True},
            {"name": "Lyons", "miles": 11.0, "needed": 22.0,
             "mountain": False, "reachable": True},
            {"name": "Boulder (Pearl St)", "miles": 17.4, "needed": 34.8,
             "mountain": False, "reachable": False},
            {"name": "Estes Park", "miles": 43.8, "needed": 87.6,
             "mountain": True, "reachable": False},
        ],
    }
