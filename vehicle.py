"""Normalize Tesla `vehicle_data` into one flat, trustworthy view model.

*** PLACEHOLDER — Task 3 supersedes this file. ***
Task 3 ("Normalize vehicle data into a view model") is blocked on a human
OAuth step needed to capture the real `tests/fixtures/vehicle_data.json`
fixture, so it has not landed yet. Task 7 (car API routes + demo data) needs
`vehicle.derive()` to exist so `car_routes.py` can import it and so the demo
vehicle_data fixture actually flows through a real derive path end to end.

This is intentionally a minimal, untested stand-in: it covers the
view-model keys documented in docs/superpowers/plans/2026-07-25-tesla-car-page.md
(Task 3, Step 2) plus `charge_power_kw`, which `store.record()` reads. It
applies the field-reference traps that are cheap and load-bearing for the
demo path (charging-state-not-voltage, climate user-intent, door/window axis
separation, the "<invalid>" sentinel, idle software-update sentinels) but
does NOT attempt the full documented shape (no seat heaters, route, tpms
warnings, capability flags, etc.) — Task 3 owns that, driven by the real
fixture and its own test suite. Do not add tests against this file; Task 3's
tests/test_vehicle.py will replace it wholesale.
"""
from __future__ import annotations

import time
from typing import Any

# Charging is a state machine, not a power reading. `charger_voltage` reads 2
# when idle, so a voltage/power heuristic reports phantom charging.
CHARGING_STATES = {"Starting", "Charging"}

# Tesla's sentinel for "no value" in enum-ish string fields.
INVALID = "<invalid>"

SOFTWARE_ACTIVE = {"available", "downloading", "downloading_wifi_wait",
                   "scheduled", "installing"}


def _s(value: Any) -> str | None:
    """String fields, with Tesla's sentinel mapped to None. `charge_port_color`
    has a genuine "Off" value, so only the explicit sentinel and blanks drop."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return None if not text or text == INVALID else text


def _open(value: Any) -> bool:
    """Door/window/trunk state. Tesla models these as bools serialized 0/1."""
    return bool(value) and value != 0


def _software(update: Any) -> dict[str, Any] | None:
    """Idle sentinels are weird: status "", version " ", install_perc 1.
    Gate on status alone."""
    if not isinstance(update, dict):
        return None
    status = (update.get("status") or "").strip()
    if status not in SOFTWARE_ACTIVE:
        return None
    return {
        "status": status,
        "version": (update.get("version") or "").strip() or None,
        "download_pct": update.get("download_perc"),
        "install_pct": update.get("install_perc"),
    }


def derive(raw: dict[str, Any]) -> dict[str, Any]:
    raw = raw or {}
    charge = raw.get("charge_state") or {}
    climate = raw.get("climate_state") or {}
    drive = raw.get("drive_state") or {}
    state = raw.get("vehicle_state") or {}

    charging_state = _s(charge.get("charging_state"))

    return {
        "vin": raw.get("vin"),
        # `display_name` does not exist in vehicle_data; the name lives here.
        "name": state.get("vehicle_name") or "Car",

        "soc": charge.get("battery_level"),
        "usable_soc": charge.get("usable_battery_level"),
        "limit": charge.get("charge_limit_soc"),
        "charging": charging_state in CHARGING_STATES,
        "charging_state": charging_state,
        "charge_power_kw": charge.get("charger_power"),
        "plugged_in": _s(charge.get("conn_charge_cable")) is not None,
        "range_mi": charge.get("battery_range"),

        # is_climate_on goes true for dog mode, COP and preconditioning. This
        # is the flag that means "the user asked for climate".
        "climate_on": bool(climate.get("is_auto_conditioning_on")),
        "inside_c": climate.get("inside_temp"),
        "outside_c": climate.get("outside_temp"),

        # These keys are ABSENT without location access, not null.
        "lat": drive.get("latitude"),
        "lon": drive.get("longitude"),
        "shift": _s(drive.get("shift_state")) or "P",
        "speed_mph": drive.get("speed") or 0,

        "odometer_mi": state.get("odometer"),
        "locked": state.get("locked"),
        "doors": {
            name: _open(state[key])
            for name, key in (("driver_front", "df"), ("driver_rear", "dr"),
                              ("passenger_front", "pf"), ("passenger_rear", "pr"))
            if key in state
        },
        "windows": {
            name: _open(state[key])
            for name, key in (("front_driver", "fd_window"),
                              ("front_passenger", "fp_window"),
                              ("rear_driver", "rd_window"),
                              ("rear_passenger", "rp_window"))
            if key in state
        },
        "sentry": state.get("sentry_mode"),
        # No API exposes footage. This is the only camera-adjacent signal there is.
        "dashcam": _s(state.get("dashcam_state")),
        "tpms_bar": {
            corner: state[f"tpms_pressure_{corner}"]
            for corner in ("fl", "fr", "rl", "rr")
            if f"tpms_pressure_{corner}" in state
        },
        "software": _software(state.get("software_update")),

        "sampled_at": int(time.time()),
    }
