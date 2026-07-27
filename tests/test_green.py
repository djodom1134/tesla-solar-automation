from __future__ import annotations

import logging
import random

import pytest

import green
import store


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


# --------------------------------------------------------------------------
# Task 18: the banked-solar ledger. Percentage points of SoC, not kWh -- see
# green.ledger_step's own docstring for the full reasoning.
# --------------------------------------------------------------------------

def test_tick_solar_w_matches_solar_kwhs_own_per_tick_formula():
    """Shared helper -- solar_kwh sums this over a tick log, the ledger only
    asks whether one tick was positive. One formula, so they cannot drift."""
    assert green.tick_solar_w(6000, -1000) == 6000
    assert green.tick_solar_w(1200, 400) == 800
    assert green.tick_solar_w(1200, 5000) == 0, "importing more than the car drew is not solar"


# Arbitrary test threshold, deliberately NOT store.GAP_SECONDS (1800) --
# ledger_step must not know or care what that constant is; see
# test_ledger_threshold_is_a_parameter_not_stores_constant below, which
# proves it rather than just asserting it by convention.
THRESH = 900


def test_ledger_starts_at_zero_and_first_tick_only_records():
    """No previous SoC exists yet -- the only honest move is to record the
    observation and bank nothing, never assume a rise or a drop happened
    before anything was watching."""
    solar_soc, stale = green.ledger_step(0.0, None, 62, True, 999_999, THRESH)
    assert solar_soc == 0
    assert stale is False


def test_ledger_rise_while_solar_charging_banks_it():
    solar_soc, stale = green.ledger_step(0.0, 50, 70, True, 60, THRESH)
    assert solar_soc == 20
    assert stale is False


def test_ledger_rise_while_grid_charging_does_not_bank():
    """Total SoC rose, so the solar FRACTION falls on its own -- grid
    electrons dilute the bank, they do not remove sun already in it."""
    solar_soc, stale = green.ledger_step(20.0, 50, 70, False, 60, THRESH)
    assert solar_soc == 20
    assert stale is False


def test_ledger_drop_drains_proportionally():
    """32 of 80 (40%) drops to 70: 40% of the 10-point drop leaves with it."""
    solar_soc, _ = green.ledger_step(32.0, 80, 70, False, 60, THRESH)
    assert solar_soc == pytest.approx(28.0)


def test_ledger_drop_to_zero_soc_leaves_zero_banked():
    solar_soc, _ = green.ledger_step(45.0, 90, 0, False, 60, THRESH)
    assert solar_soc == 0


# --------------------------------------------------------------------------
# Staleness: gap length ALONE is the wrong signal. A long gap with the SoC
# unchanged means the car sat asleep and nothing was missed -- that must
# never be flagged, or the flag fires on ordinary idle polling (this is the
# regression a prior version of this ledger actually shipped: reusing
# store.GAP_SECONDS as both the threshold AND the only signal, against a
# poll_asleep/poll_idle cadence that had independently been raised to equal
# it, meant most idle-day gaps would have tripped it for no reason).
# Staleness requires BOTH a long gap AND a SoC change across it.
# --------------------------------------------------------------------------

def test_ledger_long_gap_with_unchanged_soc_is_not_stale():
    """THE REGRESSION THAT MATTERS. The car slept the whole gap; nothing
    happened, so nothing was missed -- length alone must not flag this."""
    solar_soc, stale = green.ledger_step(12.0, 60, 60, False, THRESH + 1, THRESH)
    assert stale is False
    assert solar_soc == 12.0


def test_ledger_long_gap_with_soc_risen_and_no_solar_charging_is_stale():
    """A long gap AND a change is exactly the untrustworthy case: this
    function cannot tell whether the rise was one continuous non-solar
    charge or several ups and downs it never saw. Not solar-attributed here
    (state/attribution says grid), so the value is left unchanged -- but
    still reported stale, and still within the general clamp regardless."""
    solar_soc, stale = green.ledger_step(20.0, 50, 70, False, THRESH + 1, THRESH)
    assert stale is True
    assert 0 <= solar_soc <= 70


def test_ledger_long_gap_with_soc_fallen_is_stale_and_drains_proportionally():
    """A drop still drains proportionally regardless of staleness -- the
    proportional-drain rule doesn't get suspended by not having watched it
    happen continuously -- but the gap is still reported stale."""
    solar_soc, stale = green.ledger_step(32.0, 80, 70, False, THRESH + 1, THRESH)
    assert stale is True
    assert solar_soc == pytest.approx(28.0)


def test_ledger_short_gap_with_soc_movement_is_not_stale():
    """Ordinary accounting: seen and priced in tick by tick, regardless of
    whether the SoC moved -- only a LONG gap can ever be stale."""
    solar_soc, stale = green.ledger_step(0.0, 50, 70, True, THRESH - 1, THRESH)
    assert stale is False
    assert solar_soc == 20


def test_ledger_gap_exactly_at_the_threshold_is_not_stale():
    """Strictly greater than, not greater-or-equal."""
    _, stale = green.ledger_step(10.0, 50, 55, True, THRESH, THRESH)
    assert stale is False


def test_ledger_threshold_is_a_parameter_not_stores_constant(monkeypatch):
    """gap_threshold_s must be the ONLY threshold ledger_step consults --
    proven, not just asserted, by wrecking store.GAP_SECONDS and confirming
    it has no effect whatsoever on the result."""
    monkeypatch.setattr(store, "GAP_SECONDS", 1)
    _, stale = green.ledger_step(10.0, 50, 55, True, gap_s=100,
                                 gap_threshold_s=1800)
    assert stale is False, "must ignore store.GAP_SECONDS entirely"


def test_ledger_clamp_logs_a_warning(caplog):
    """A clamp that actually changes the value means a sample was missed or
    the car charged somewhere unobserved -- a silent clamp would hide it."""
    with caplog.at_level(logging.WARNING, logger="green"):
        # delta == 0 leaves solar_soc unchanged pre-clamp; an already-
        # inconsistent input (25 > soc_now of 20) is what makes the general
        # clamp actually fire, independent of staleness.
        green.ledger_step(25.0, 20, 20, False, THRESH + 1, THRESH)
    assert any("clamp" in r.message for r in caplog.records)


def test_ledger_solar_soc_never_exceeds_soc():
    """Property check across a long random walk of rises, drops, gaps and
    engagement flags -- not just the hand-picked examples above."""
    rng = random.Random(20260726)
    solar_soc: float = 0.0
    soc_before: int | None = None
    soc = 50
    for _ in range(2000):
        soc = max(0, min(100, soc + rng.randint(-20, 20)))
        charging = rng.random() < 0.5
        gap = rng.randint(0, 4000)
        solar_soc, stale = green.ledger_step(
            solar_soc, soc_before, soc, charging, gap, THRESH)
        assert 0 <= solar_soc <= soc, (solar_soc, soc_before, soc, charging, gap, stale)
        soc_before = soc


def test_ledger_full_cycle_bank_drive_off_half_bank_again():
    """Hand arithmetic: bank 20 (0 -> 20 of 70); halve the pack by driving
    (70 -> 35), which -- proportional drain preserving the ratio -- halves
    the bank too (20 -> 10); bank 15 more (10 -> 25 of 50)."""
    solar_soc, stale = green.ledger_step(0.0, 50, 70, True, 60, THRESH)
    assert solar_soc == 20 and stale is False

    solar_soc, _ = green.ledger_step(solar_soc, 70, 35, False, 60, THRESH)
    assert solar_soc == pytest.approx(10.0)

    solar_soc, _ = green.ledger_step(solar_soc, 35, 50, True, 60, THRESH)
    assert solar_soc == pytest.approx(25.0)


def test_banked_miles_rated_uses_the_cars_own_range():
    assert green.banked_miles_rated(20.0, 80, 280.0) == pytest.approx(70.0)


def test_banked_miles_rated_is_a_definite_zero_when_nothing_is_banked():
    """Today's honest output: solar_soc is 0, so the rated figure is a real
    zero, not an unknown -- there is nothing missing here."""
    assert green.banked_miles_rated(0.0, 72, 250.0) == 0


def test_banked_miles_rated_is_none_when_soc_or_range_is_unknown():
    assert green.banked_miles_rated(20.0, None, 280.0) is None
    assert green.banked_miles_rated(20.0, 0, 280.0) is None
    assert green.banked_miles_rated(20.0, 80, None) is None


def test_banked_miles_measured_is_none_until_thresholds_met():
    assert green.banked_miles_measured(20.0, None, 3.5) is None
    assert green.banked_miles_measured(20.0, 80.0, None) is None


def test_banked_miles_measured_multiplies_the_bank_by_real_consumption():
    assert green.banked_miles_measured(20.0, 80.0, 3.5) == pytest.approx(56.0)
