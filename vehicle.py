"""Normalize Tesla `vehicle_data` into one flat, trustworthy view model.

Tesla's payload mixes metric temperatures with imperial distances, uses two
opposite naming conventions for doors and windows, and omits whole keys rather
than nulling them. Every trap encoded here is documented in
docs/tesla-field-reference.md — read that before changing a field access.
"""
from __future__ import annotations

import time
from typing import Any

# Charging is a state machine, not a power reading. `charger_voltage` reads 2
# when idle, so any voltage/power heuristic reports phantom charging.
CHARGING_STATES = {"Starting", "Charging"}

# Tesla's sentinel for "no value" in enum-ish string fields.
INVALID = "<invalid>"

SOFTWARE_ACTIVE = {"available", "downloading", "downloading_wifi_wait",
                   "scheduled", "installing"}


def _s(value: Any) -> str | None:
    """String fields, with Tesla's sentinel mapped to None.

    `charge_port_color` has a genuine "Off" value, so only the explicit
    sentinel and blanks are dropped."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return None if not text or text == INVALID else text


def _open(value: Any) -> bool:
    """Door/window/trunk state. Tesla models these as bools serialized 0/1."""
    return bool(value) and value != 0


def derive(raw: dict[str, Any]) -> dict[str, Any]:
    raw = raw or {}
    charge = raw.get("charge_state") or {}
    climate = raw.get("climate_state") or {}
    drive = raw.get("drive_state") or {}
    state = raw.get("vehicle_state") or {}
    config = raw.get("vehicle_config") or {}

    charging_state = _s(charge.get("charging_state"))

    return {
        "vin": raw.get("vin"),
        # `display_name` does not exist in vehicle_data; the name lives here.
        "name": state.get("vehicle_name") or "Car",
        "state": raw.get("state"),
        "version": (state.get("car_version") or "").split(" ")[0] or None,

        # ---- charge ----
        "soc": charge.get("battery_level"),
        "usable_soc": charge.get("usable_battery_level"),
        "limit": charge.get("charge_limit_soc"),
        "limit_min": charge.get("charge_limit_soc_min"),
        "limit_max": charge.get("charge_limit_soc_max"),
        "charging": charging_state in CHARGING_STATES,
        "charging_state": charging_state,
        "charge_power_kw": charge.get("charger_power"),
        "charge_amps": charge.get("charge_current_request"),
        "amps_max": charge.get("charge_current_request_max"),
        "amps_actual": charge.get("charger_actual_current"),
        "minutes_to_full": charge.get("minutes_to_full_charge")
            if isinstance(charge.get("minutes_to_full_charge"), (int, float)) else None,
        "energy_added_kwh": charge.get("charge_energy_added"),
        "range_mi": charge.get("battery_range"),
        "range_est_mi": charge.get("est_battery_range"),
        "plugged_in": _s(charge.get("conn_charge_cable")) is not None,
        "port_open": bool(charge.get("charge_port_door_open")),
        "port_latch": _s(charge.get("charge_port_latch")),
        "port_color": _s(charge.get("charge_port_color")),
        "fast_charger": _s(charge.get("fast_charger_type")),
        "fast_charger_present": charge.get("fast_charger_present"),
        # charger_voltage reads 2, not 0, when idle -- so it is only meaningful
        # mid-session. The solar controller converts amps to watts with it.
        "volts": (charge.get("charger_voltage")
                  if charging_state in CHARGING_STATES else None),
        "scheduled_mode": _s(charge.get("scheduled_charging_mode")),

        # ---- climate ----
        "inside_c": climate.get("inside_temp"),
        "outside_c": climate.get("outside_temp"),
        # is_climate_on goes true for dog mode, COP and preconditioning. This
        # is the flag that means "the user asked for climate".
        "climate_on": bool(climate.get("is_auto_conditioning_on")),
        "climate_any": bool(climate.get("is_climate_on")),
        "preconditioning": bool(climate.get("is_preconditioning")),
        "climate_keeper": _s(climate.get("climate_keeper_mode")),
        "defrost": climate.get("defrost_mode"),
        "driver_temp_c": climate.get("driver_temp_setting"),
        "passenger_temp_c": climate.get("passenger_temp_setting"),
        "temp_min_c": climate.get("min_avail_temp"),
        "temp_max_c": climate.get("max_avail_temp"),
        "seat_heaters": {
            name: climate[key]
            for name, key in (
                ("front_left", "seat_heater_left"),
                ("front_right", "seat_heater_right"),
                ("rear_left", "seat_heater_rear_left"),
                ("rear_center", "seat_heater_rear_center"),
                ("rear_right", "seat_heater_rear_right"),
            )
            if key in climate
        },
        "wheel_heater": climate.get("steering_wheel_heater"),
        # Read-only vehicle-side gate. When false, every remote comfort
        # command comes back result:false and there is no command to flip it.
        "comfort_enabled": climate.get("remote_heater_control_enabled", True),

        # ---- position ----
        # These keys are ABSENT without location access, not null.
        "lat": drive.get("latitude"),
        "lon": drive.get("longitude"),
        "heading": drive.get("heading"),
        "speed_mph": drive.get("speed") or 0,
        "shift": _s(drive.get("shift_state")) or "P",
        "power_kw": drive.get("power"),
        "gps_at": drive.get("gps_as_of"),
        "route": _route(drive),

        # ---- body ----
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
        "trunks": {
            name: _open(state[key])
            for name, key in (("front", "ft"), ("rear", "rt")) if key in state
        },
        "sentry": state.get("sentry_mode"),
        "sentry_available": bool(state.get("sentry_mode_available")),
        # No API exposes footage. This is the only camera-adjacent signal there is.
        "dashcam": _s(state.get("dashcam_state")),
        "user_present": bool(state.get("is_user_present")),
        "valet": bool(state.get("valet_mode")),
        "speed_limit": state.get("speed_limit_mode"),
        "tpms_bar": {
            corner: state[f"tpms_pressure_{corner}"]
            for corner in ("fl", "fr", "rl", "rr")
            if f"tpms_pressure_{corner}" in state
        },
        "tpms_warn": {
            corner: bool(state.get(f"tpms_soft_warning_{corner}")
                         or state.get(f"tpms_hard_warning_{corner}"))
            for corner in ("fl", "fr", "rl", "rr")
            if f"tpms_pressure_{corner}" in state
        },
        "software": _software(state.get("software_update")),
        "homelink_nearby": state.get("homelink_nearby"),
        "homelink_devices": state.get("homelink_device_count"),

        # ---- capability flags, for hiding controls the car cannot do ----
        "has_sunroof": bool(config.get("sun_roof_installed")),
        "has_seat_cooling": bool(config.get("has_seat_cooling")),
        "has_rear_seat_heaters": bool(config.get("rear_seat_heaters")),

        "sampled_at": int(time.time()),
    }


def _route(drive: dict[str, Any]) -> dict[str, Any] | None:
    """Active navigation. These keys vanish individually, not as a group."""
    minutes = drive.get("active_route_minutes_to_arrival")
    if minutes is None:
        return None
    return {
        "destination": drive.get("active_route_destination"),
        "lat": drive.get("active_route_latitude"),
        "lon": drive.get("active_route_longitude"),
        "miles": drive.get("active_route_miles_to_arrival"),
        "minutes": minutes,
        "delay_minutes": drive.get("active_route_traffic_minutes_delay"),
        "soc_at_arrival": drive.get("active_route_energy_at_arrival"),
    }


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
        "duration_sec": update.get("expected_duration_sec"),
    }
