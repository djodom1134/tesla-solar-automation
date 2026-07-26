"""Derive import/export metrics from Fleet API `calendar_history?kind=energy` rows.

All Fleet API energy values are watt-hours; everything here returns kilowatt-hours.

The raw schema splits every flow by source and destination, which is what makes a
truthful import/export view possible:

    grid  -> home     consumer_energy_imported_from_grid
    grid  -> battery  battery_energy_imported_from_grid
    solar -> home     consumer_energy_imported_from_solar
    solar -> battery  battery_energy_imported_from_solar
    solar -> grid     grid_energy_exported_from_solar
    batt  -> home     consumer_energy_imported_from_battery
    batt  -> grid     grid_energy_exported_from_battery

`grid_energy_imported` is the total pulled from the grid and already includes the
battery's share, so it must not be added to the consumer figure.
"""
from __future__ import annotations

from typing import Any

WH_PER_KWH = 1000.0


def _wh(row: dict[str, Any], *keys: str) -> float:
    return sum(float(row.get(k) or 0) for k in keys)


def derive(row: dict[str, Any]) -> dict[str, Any]:
    """One calendar_history time_series entry -> the numbers the dashboard shows (kWh)."""
    home_from_solar = _wh(row, "consumer_energy_imported_from_solar")
    home_from_battery = _wh(row, "consumer_energy_imported_from_battery")
    home_from_grid = _wh(row, "consumer_energy_imported_from_grid")
    home_from_generator = _wh(row, "consumer_energy_imported_from_generator")
    home = home_from_solar + home_from_battery + home_from_grid + home_from_generator

    solar = _wh(row, "solar_energy_exported")

    # Total grid import, including whatever went straight into the battery.
    grid_import = _wh(row, "grid_energy_imported")

    # Export has no single total field — sum it by origin.
    grid_export = _wh(
        row,
        "grid_energy_exported_from_solar",
        "grid_energy_exported_from_battery",
        "grid_energy_exported_from_generator",
    )

    battery_charged = _wh(
        row,
        "battery_energy_imported_from_solar",
        "battery_energy_imported_from_grid",
        "battery_energy_imported_from_generator",
    )
    battery_discharged = _wh(row, "battery_energy_exported")

    grid_export_from_solar = _wh(row, "grid_energy_exported_from_solar")

    # Share of the home's consumption that never touched the grid.
    self_sufficiency = ((home - home_from_grid) / home * 100) if home > 0 else None
    # Share of generated solar consumed on site rather than exported.
    self_consumption = (
        ((solar - grid_export_from_solar) / solar * 100) if solar > 0 else None
    )

    kwh = lambda v: round(v / WH_PER_KWH, 3)  # noqa: E731

    return {
        "timestamp": row.get("timestamp"),
        "solar": kwh(solar),
        "home": kwh(home),
        "home_from_solar": kwh(home_from_solar),
        "home_from_battery": kwh(home_from_battery),
        "home_from_grid": kwh(home_from_grid),
        "home_from_generator": kwh(home_from_generator),
        "grid_import": kwh(grid_import),
        "grid_export": kwh(grid_export),
        "grid_export_from_solar": kwh(grid_export_from_solar),
        "grid_net": kwh(grid_export - grid_import),  # positive = net exporter
        "battery_charged": kwh(battery_charged),
        "battery_discharged": kwh(battery_discharged),
        "self_sufficiency": round(self_sufficiency, 1) if self_sufficiency is not None else None,
        "self_consumption": round(self_consumption, 1) if self_consumption is not None else None,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals across derived rows. Ratios are recomputed from the totals, never averaged —
    averaging per-day percentages would weight a cloudy 2 kWh day the same as a sunny 40 kWh one."""
    if not rows:
        return {}

    total = {
        key: round(sum(r[key] for r in rows), 3)
        for key in (
            "solar",
            "home",
            "home_from_solar",
            "home_from_battery",
            "home_from_grid",
            "home_from_generator",
            "grid_import",
            "grid_export",
            "battery_charged",
            "battery_discharged",
        )
    }
    total["grid_net"] = round(total["grid_export"] - total["grid_import"], 3)

    home = total["home"]
    solar = total["solar"]
    total["self_sufficiency"] = (
        round((home - total["home_from_grid"]) / home * 100, 1) if home > 0 else None
    )
    # Exported-from-solar isn't kept per row, so approximate with total export capped at solar.
    exported_solar = min(total["grid_export"], solar)
    total["self_consumption"] = (
        round((solar - exported_solar) / solar * 100, 1) if solar > 0 else None
    )
    return total


def money(total: dict[str, Any], import_rate: float | None, export_rate: float | None) -> dict | None:
    """Optional cost readout. Only shown when the user actually set rates in .env."""
    if not total or (import_rate is None and export_rate is None):
        return None
    cost = (total.get("grid_import", 0) or 0) * (import_rate or 0)
    credit = (total.get("grid_export", 0) or 0) * (export_rate or 0)
    return {
        "cost": round(cost, 2),
        "credit": round(credit, 2),
        "net": round(credit - cost, 2),  # positive = the grid owes you
    }


def live(status: dict[str, Any]) -> dict[str, Any]:
    """live_status -> instantaneous power in kW.

    Tesla's sign conventions: grid_power > 0 means importing, < 0 means exporting.
    battery_power > 0 means discharging, < 0 means charging.
    """
    kw = lambda v: round(float(v or 0) / 1000.0, 2)  # noqa: E731

    grid_power = float(status.get("grid_power") or 0)
    battery_power = float(status.get("battery_power") or 0)

    return {
        "solar": kw(status.get("solar_power")),
        "home": kw(status.get("load_power")),
        "grid": kw(grid_power),
        "grid_direction": "import" if grid_power > 50 else "export" if grid_power < -50 else "idle",
        "battery": kw(battery_power),
        "battery_direction": (
            "discharging" if battery_power > 50 else "charging" if battery_power < -50 else "idle"
        ),
        "battery_percent": (
            round(float(status["percentage_charged"]), 1)
            if status.get("percentage_charged") is not None
            else None
        ),
        "energy_left_kwh": kw(status.get("energy_left")),
        "grid_status": status.get("grid_status"),
        "storm_mode_active": status.get("storm_mode_active"),
        "island_status": status.get("island_status"),
        "timestamp": status.get("timestamp"),
    }


def power_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`kind=power` samples -> kW, with the grid split into import/export so the
    intraday chart can diverge around zero."""
    out = []
    for row in rows:
        grid = float(row.get("grid_power") or 0) / 1000.0
        solar = float(row.get("solar_power") or 0) / 1000.0
        battery = float(row.get("battery_power") or 0) / 1000.0
        out.append(
            {
                "timestamp": row.get("timestamp"),
                "solar": round(solar, 3),
                "battery": round(battery, 3),
                # The power feed carries no load channel, but the site balance gives it:
                # load = solar + grid + battery (grid > 0 import, battery > 0 discharge).
                "home": round(solar + grid + battery, 3),
                "grid_import": round(max(grid, 0.0), 3),
                "grid_export": round(max(-grid, 0.0), 3),
            }
        )
    return out
