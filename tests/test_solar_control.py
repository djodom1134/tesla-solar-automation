from __future__ import annotations

import pytest

import solar

T = solar.Tunables(margin_w=100, deadband_w=250, ramp_a=8, min_a=5, max_a=48, volts=240)


def test_car_watts_is_zero_unless_actually_charging():
    """In idle and stopped the car draws nothing. Using the standing amps
    setting instead would invent 1.2-11.5 kW of surplus that does not exist."""
    assert solar.car_watts({"charging_state": "Stopped", "amps_actual": 32}, 240) == 0
    assert solar.car_watts({"charging_state": "Disconnected", "amps_actual": 32}, 240) == 0
    assert solar.car_watts({"charging_state": "Complete", "amps_actual": 0}, 240) == 0
    assert solar.car_watts({"charging_state": "Charging", "amps_actual": 20}, 240) == 4800
    assert solar.car_watts({"charging_state": "Starting", "amps_actual": 5}, 240) == 1200


def test_car_watts_is_zero_when_amps_unknown():
    assert solar.car_watts({"charging_state": "Charging", "amps_actual": None}, 240) == 0


def test_surplus_is_absolute_and_includes_the_cars_own_draw():
    # Car pulling 9.6 kW with the meter at zero: all 9.6 kW is solar.
    assert solar.surplus_watts(9600, 0) == 9600
    # Car off, exporting 3 kW: 3 kW is available.
    assert solar.surplus_watts(0, -3000) == 3000
    # Car pulling 1.2 kW while importing 400 W: only 800 W is solar.
    assert solar.surplus_watts(1200, 400) == 800


@pytest.mark.parametrize("name,grid_w,current_a,expect_target,expect_breach", [
    # The converged states MUST NOT breach. This is the regression that a
    # previous design failed: a perfectly charging car read as below floor.
    ("converged 3kW sun at 12A",      -120, 12, 12, False),
    ("converged 9.6kW sun at 40A",       0, 40, 40, False),
    ("sun rising, headroom at 12A",  -2000, 12, 20, False),
    ("AC starts, must back off",      1500, 20, 13, False),
    ("cloud, genuine floor breach",    400,  5,  5, True),
    ("ramp limit caps the climb",    -5000, 10, 18, False),
    ("clamps to max",               -20000, 45, 48, False),
])
def test_control_law(name, grid_w, current_a, expect_target, expect_breach):
    d = solar.control(grid_w, current_a, T)
    assert d.target_a == expect_target, name
    assert d.floor_breach is expect_breach, name


def test_deadband_suppresses_the_write():
    d = solar.control(-100, 20, T)          # error_w = 0
    assert d.write is False
    assert d.target_a == 20


def test_outside_the_deadband_writes():
    d = solar.control(-500, 20, T)          # error_w = 400 > 250
    assert d.write is True
    assert d.target_a == 22


def test_target_is_always_an_integer():
    d = solar.control(-333, 11, T)
    assert isinstance(d.target_a, int)


def test_raw_target_is_unclamped_so_the_floor_test_can_see_below_min():
    d = solar.control(400, 5, T)            # importing 400 W at the floor
    assert d.raw_target < T.min_a
    assert d.target_a == T.min_a            # but we never COMMAND below min
