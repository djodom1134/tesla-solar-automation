from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import backtest
import solar


def bucket(imported=0, exported_solar=0, solar_wh=0):
    return {"grid_energy_imported": imported,
            "grid_energy_exported_from_solar": exported_solar,
            "grid_energy_exported_from_battery": 0,
            "grid_energy_exported_from_generator": 0,
            "solar_energy_exported": solar_wh}


def test_energy_buckets_convert_to_average_watts():
    """calendar_history returns Wh per 5 minutes; the controller wants watts.
    500 Wh over 5 minutes is 6000 W."""
    assert backtest.grid_watts(bucket(imported=500)) == 6000.0
    assert backtest.grid_watts(bucket(exported_solar=500)) == -6000.0


def test_a_sunny_day_charges_without_importing():
    day = [bucket(exported_solar=800, solar_wh=1000) for _ in range(96)]
    out = backtest.simulate(day, {})
    assert out["imported_wh"] == 0
    assert out["captured_kwh"] > 0


def test_a_dark_day_never_starts():
    day = [bucket(imported=300) for _ in range(96)]
    out = backtest.simulate(day, {})
    assert out["captured_kwh"] == 0
    assert out["stop_starts"] == 0
