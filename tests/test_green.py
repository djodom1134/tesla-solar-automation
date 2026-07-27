from __future__ import annotations

import pytest

import green


def test_solar_kwh_subtracts_concurrent_grid_import():
    """The controller is the only thing modulating the car, so within a tick
    the attribution is exact rather than pro-rata: whatever the car drew, minus
    whatever was being imported at that moment, came from the sun.

    Conservative by construction -- import is charged wholly against the car
    even though some of it fed the house. A lower bound is the right kind of
    wrong for a number whose whole purpose is to be trustworthy."""
    ticks = [
        {"state": "charging", "car_w": 6000, "grid_w": -1000, "period_s": 120},
        {"state": "grace",    "car_w": 1200, "grid_w":   400, "period_s": 120},
        {"state": "idle",     "car_w":    0, "grid_w":  3000, "period_s": 120},
    ]
    # 6000 W of solar for 120 s, then (1200-400)=800 W, then nothing.
    assert green.solar_kwh(ticks) == pytest.approx((6000 + 800) * 120 / 3600 / 1000)


def test_solar_kwh_never_goes_negative():
    """Importing more than the car draws means none of it was solar."""
    assert green.solar_kwh([
        {"state": "grace", "car_w": 1200, "grid_w": 5000, "period_s": 120}]) == 0


def test_solar_kwh_ignores_ticks_where_the_car_was_not_charging():
    assert green.solar_kwh([
        {"state": "idle", "car_w": 9000, "grid_w": -9000, "period_s": 120}]) == 0


def test_pack_size_needs_more_than_one_session():
    """One session at integer-percent SoC carries a ~5 kWh error bar. Reporting
    it as fact would put a fabricated precision under every other number."""
    one = [{"soc_start": 64, "soc_end": 79, "kwh_added": 12.16}]
    pack, n = green.pack_kwh(one)
    assert pack is None and n == 1


def test_pack_size_emerges_from_several_sessions():
    sessions = [
        {"soc_start": 64, "soc_end": 79, "kwh_added": 12.16},
        {"soc_start": 30, "soc_end": 80, "kwh_added": 40.5},
        {"soc_start": 45, "soc_end": 90, "kwh_added": 36.4},
    ]
    pack, n = green.pack_kwh(sessions)
    assert n == 3
    assert 78 < pack < 84


def test_pack_size_ignores_sessions_too_small_to_be_informative():
    """A 2% swing divides by a number whose own error is 50%."""
    pack, n = green.pack_kwh([{"soc_start": 78, "soc_end": 80, "kwh_added": 1.6}] * 5)
    assert pack is None and n == 0


def test_miles_per_kwh_is_none_without_enough_distance():
    assert green.miles_per_kwh([{"miles": 12.0, "soc_drop": 5}], 80.0)[0] is None


def test_miles_per_kwh_from_real_driving():
    segs = [{"miles": 120.0, "soc_drop": 40}, {"miles": 90.0, "soc_drop": 31}]
    mpk, miles = green.miles_per_kwh(segs, 80.0)
    assert miles == 210.0
    assert 3.0 < mpk < 4.0


def test_free_miles_is_none_when_either_input_is_unknown():
    assert green.free_miles(5.0, None) is None
    assert green.free_miles(None, 3.5) is None


def test_free_miles_multiplies_when_both_are_known():
    assert green.free_miles(10.0, 3.5) == pytest.approx(35.0)
