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
    solar_soc, stale = green.ledger_step(0.0, None, 62, 1.0, 999_999, THRESH)
    assert solar_soc == 0
    assert stale is False


def test_ledger_rise_while_solar_charging_banks_it():
    solar_soc, stale = green.ledger_step(0.0, 50, 70, 1.0, 60, THRESH)
    assert solar_soc == 20
    assert stale is False


def test_ledger_rise_while_grid_charging_does_not_bank():
    """Total SoC rose, so the solar FRACTION falls on its own -- grid
    electrons dilute the bank, they do not remove sun already in it."""
    solar_soc, stale = green.ledger_step(20.0, 50, 70, 0.0, 60, THRESH)
    assert solar_soc == 20
    assert stale is False


def test_ledger_drop_spends_the_sun_first():
    """The owner's rule (2026-09-19): banked sun is spent on the next miles
    driven, not drained pro rata. 32 banked, a 10-point drop: the bank pays
    all ten and 22 is left -- not the 28 a proportional drain would leave."""
    solar_soc, _ = green.ledger_step(32.0, 80, 70, 0.0, 60, THRESH)
    assert solar_soc == pytest.approx(22.0)


def test_ledger_drop_larger_than_the_bank_empties_it_and_no_further():
    """5 banked against a 12-point drop: the bank covers five of them and
    the grid share covers the rest. Never negative."""
    solar_soc, _ = green.ledger_step(5.0, 80, 68, 0.0, 60, THRESH)
    assert solar_soc == 0


def test_ledger_drop_to_zero_soc_leaves_zero_banked():
    solar_soc, _ = green.ledger_step(45.0, 90, 0, 0.0, 60, THRESH)
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
    solar_soc, stale = green.ledger_step(12.0, 60, 60, 0.0, THRESH + 1, THRESH)
    assert stale is False
    assert solar_soc == 12.0


def test_ledger_long_gap_with_soc_risen_and_no_solar_charging_is_stale():
    """A long gap AND a change is exactly the untrustworthy case: this
    function cannot tell whether the rise was one continuous non-solar
    charge or several ups and downs it never saw. Not solar-attributed here
    (state/attribution says grid), so the value is left unchanged -- but
    still reported stale, and still within the general clamp regardless."""
    solar_soc, stale = green.ledger_step(20.0, 50, 70, 0.0, THRESH + 1, THRESH)
    assert stale is True
    assert 0 <= solar_soc <= 70


def test_ledger_long_gap_with_soc_fallen_is_stale_and_still_spends_sun_first():
    """A drop still spends the bank regardless of staleness -- the rule is
    not suspended by not having watched it happen continuously -- but the
    gap is still reported stale."""
    solar_soc, stale = green.ledger_step(32.0, 80, 70, 0.0, THRESH + 1, THRESH)
    assert stale is True
    assert solar_soc == pytest.approx(22.0)


def test_ledger_short_gap_with_soc_movement_is_not_stale():
    """Ordinary accounting: seen and priced in tick by tick, regardless of
    whether the SoC moved -- only a LONG gap can ever be stale."""
    solar_soc, stale = green.ledger_step(0.0, 50, 70, 1.0, THRESH - 1, THRESH)
    assert stale is False
    assert solar_soc == 20


def test_ledger_gap_exactly_at_the_threshold_is_not_stale():
    """Strictly greater than, not greater-or-equal."""
    _, stale = green.ledger_step(10.0, 50, 55, 1.0, THRESH, THRESH)
    assert stale is False


def test_ledger_threshold_is_a_parameter_not_stores_constant(monkeypatch):
    """gap_threshold_s must be the ONLY threshold ledger_step consults --
    proven, not just asserted, by wrecking store.GAP_SECONDS and confirming
    it has no effect whatsoever on the result."""
    monkeypatch.setattr(store, "GAP_SECONDS", 1)
    _, stale = green.ledger_step(10.0, 50, 55, 1.0, gap_s=100,
                                 gap_threshold_s=1800)
    assert stale is False, "must ignore store.GAP_SECONDS entirely"


def test_ledger_clamp_logs_a_warning(caplog):
    """A clamp that actually changes the value means a sample was missed or
    the car charged somewhere unobserved -- a silent clamp would hide it."""
    with caplog.at_level(logging.WARNING, logger="green"):
        # delta == 0 leaves solar_soc unchanged pre-clamp; an already-
        # inconsistent input (25 > soc_now of 20) is what makes the general
        # clamp actually fire, independent of staleness.
        green.ledger_step(25.0, 20, 20, 0.0, THRESH + 1, THRESH)
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


def test_ledger_full_cycle_bank_drive_off_bank_again():
    """Hand arithmetic on the sun-first rule: bank 20 (0 -> 20 of 70); drive
    35 points off, which the 20 banked cannot cover, so the bank empties and
    the grid share pays the other 15; then bank 15 more (0 -> 15 of 50)."""
    solar_soc, stale = green.ledger_step(0.0, 50, 70, 1.0, 60, THRESH)
    assert solar_soc == 20 and stale is False

    solar_soc, _ = green.ledger_step(solar_soc, 70, 35, 0.0, 60, THRESH)
    assert solar_soc == 0

    solar_soc, _ = green.ledger_step(solar_soc, 35, 50, 1.0, 60, THRESH)
    assert solar_soc == pytest.approx(15.0)


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


# --------------------------------------------------------------------------
# Task 20: lifetime free miles driven. No new physics -- the banked-solar
# ledger above already knows, every tick, what fraction of the pack came
# from the sun; this only multiplies that fraction by the miles driven
# since the last observation and keeps a running lifetime total.
# --------------------------------------------------------------------------

def test_free_miles_first_tick_after_deployment_records_but_tracks_nothing():
    """No previous odometer exists yet -- the only honest move is to record
    it and accumulate nothing, never assume driving happened before
    anything was watching. Never backfilled from history."""
    free, tracked, odo = green.free_miles_step(0.0, 0.0, None, 55_000.0, 20.0, 80)
    assert (free, tracked, odo) == (0.0, 0.0, 55_000.0)


def test_free_miles_spends_the_bank_first_on_the_miles_just_driven():
    """The owner's rule: 20 banked points against a 4-point drop over 20
    miles -- the bank covers the whole drop, so every one of those miles is
    free. The old proportional rule called 25% of them free."""
    free, tracked, odo = green.free_miles_step(0.0, 0.0, 500.0, 520.0, 20.0, 80, 76)
    assert free == pytest.approx(20.0)
    assert tracked == 20.0
    assert odo == 520.0


def test_free_miles_splits_the_window_when_the_bank_runs_out_mid_drive():
    """2 banked points against a 4-point drop over 16 miles: the bank pays
    for half the energy, so half the miles are free."""
    free, tracked, _ = green.free_miles_step(0.0, 0.0, 500.0, 516.0, 2.0, 77, 73)
    assert free == pytest.approx(8.0)
    assert tracked == pytest.approx(16.0)


def test_free_miles_credits_nothing_when_the_pack_did_not_fall():
    """Miles driven but the SoC came out level (charged as much as driven,
    or the window spans both): two readings cannot attribute that energy, so
    the miles are tracked and no sun is claimed. Understating the sun's
    share is the honest direction."""
    free, tracked, odo = green.free_miles_step(0.0, 0.0, 500.0, 510.0, 20.0, 80, 80)
    assert (free, tracked, odo) == (0.0, 10.0, 510.0)


def test_free_miles_is_a_running_lifetime_total_across_many_windows():
    """Lifetime totals, not reset per call -- two windows with different
    solar shares must sum, not overwrite."""
    # 20 miles on a 4-point drop the 20-point bank covers: all free.
    free, tracked, odo = green.free_miles_step(0.0, 0.0, 500.0, 520.0, 20.0, 80, 76)
    # 20 more on an 8-point drop the 4 points left cover half of: 10 free.
    free, tracked, odo = green.free_miles_step(free, tracked, odo, 540.0, 4.0, 76, 68)
    assert free == pytest.approx(30.0)
    assert tracked == pytest.approx(40.0)
    assert odo == 540.0


def test_free_miles_ignores_negative_odometer_delta_and_holds_the_baseline(caplog):
    """The odometer only counts up -- a drop is a corrupt or reordered
    sample, not a car that reversed its own lifetime mileage. Silently
    subtracting it would make the total wrong in a way nobody could audit,
    so the whole observation (including the baseline) is ignored."""
    with caplog.at_level(logging.WARNING, logger="green"):
        free, tracked, odo = green.free_miles_step(10.0, 300.0, 500.0, 480.0, 20.0, 80)
    assert (free, tracked, odo) == (10.0, 300.0, 500.0)
    assert any("backward" in r.message for r in caplog.records)


def test_free_miles_recovers_after_a_corrupt_sample_without_double_counting():
    """The baseline held back by the corrupt sample above means the NEXT
    good sample's window naturally merges whatever was skipped -- no miles
    invented, none lost."""
    free, tracked, odo = green.free_miles_step(10.0, 300.0, 500.0, 480.0, 20.0, 80, 78)
    free, tracked, odo = green.free_miles_step(free, tracked, odo, 510.0, 20.0, 80, 78)
    assert tracked == pytest.approx(300.0 + 10.0), "500 -> 510 is a 10-mile window"
    assert free == pytest.approx(10.0 + 10.0), "a 2-point drop the bank covers"


def test_free_miles_skips_the_window_when_soc_before_is_zero_but_still_advances_odo():
    """Nothing to take a fraction of -- but the odometer reading itself is
    still trustworthy, so the baseline still advances rather than merging
    this window into the next one (unlike the corrupt-sample case above)."""
    free, tracked, odo = green.free_miles_step(10.0, 300.0, 500.0, 520.0, 20.0, 0, 0)
    assert (free, tracked, odo) == (10.0, 300.0, 520.0)


def test_free_miles_skips_the_window_when_soc_before_is_none_but_still_advances_odo():
    """Defensive: the banked-solar ledger hasn't observed its own first
    tick yet. Cannot happen via the real collector wiring today (ledger_odo
    and ledger_soc are always set together), but the pure function must not
    divide by an unknown regardless."""
    free, tracked, odo = green.free_miles_step(10.0, 300.0, 500.0, 520.0, 20.0, None, 0)
    assert (free, tracked, odo) == (10.0, 300.0, 520.0)


def test_free_miles_zero_delta_window_changes_nothing():
    free, tracked, odo = green.free_miles_step(10.0, 300.0, 500.0, 500.0, 20.0, 80, 78)
    assert (free, tracked, odo) == (10.0, 300.0, 500.0)


def test_free_miles_never_exceeds_tracked_miles():
    """Property check across a long random walk -- solar_share is always a
    fraction (solar_soc <= soc by the banked ledger's own invariant), so the
    lifetime free total can never outrun the lifetime tracked total."""
    rng = random.Random(20260727)
    free, tracked = 0.0, 0.0
    odo: float | None = None
    for _ in range(2000):
        odo_now = (odo or 50_000.0) + rng.uniform(0, 5)
        soc_before = rng.randint(1, 100)
        solar_soc_before = rng.uniform(0, soc_before)
        free, tracked, odo = green.free_miles_step(
            free, tracked, odo, odo_now, solar_soc_before, soc_before)
        assert free <= tracked + 1e-9, (free, tracked, odo_now, soc_before, solar_soc_before)


def test_accrual_is_zero_without_solar_draw():
    assert green.accrual_mi_per_s(0.0, 3.5, 38, 107.8, 100.0) == (0.0, "none")
    assert green.accrual_mi_per_s(-500.0, 3.5, 38, 107.8, 100.0) == (0.0, "none")


def test_accrual_prefers_the_owners_measured_consumption():
    rate, basis = green.accrual_mi_per_s(2430.0, 3.5, 38, 107.8, 100.0)
    assert basis == "measured"
    # 2.43 kW x 3.5 mi/kWh = 8.505 mi/h = 0.002363 mi/s
    assert rate == pytest.approx(2.43 * 3.5 / 3600, rel=1e-6)


def test_accrual_falls_back_to_the_cars_own_rated_range():
    """Before two charge sessions exist there is no measured mi/kWh, so the
    rate comes from the car's rated range over a nominal pack -- and must say
    so, because the ledger itself never depends on pack size."""
    rate, basis = green.accrual_mi_per_s(2430.0, None, 38, 107.8, None)
    assert basis == "rated"
    full_rated = 107.8 / 38 * 100          # ~283.7 mi
    expect = 2.43 * (full_rated / green.NOMINAL_PACK_KWH) / 3600
    assert rate == pytest.approx(expect, rel=1e-6)


def test_accrual_gives_up_rather_than_guessing():
    assert green.accrual_mi_per_s(2430.0, None, None, 107.8, None) == (0.0, "none")
    assert green.accrual_mi_per_s(2430.0, None, 0, 107.8, None) == (0.0, "none")
    assert green.accrual_mi_per_s(2430.0, None, 38, None, None) == (0.0, "none")


def test_a_hundredth_of_a_mile_is_seconds_not_milliseconds():
    """Grounds the UI decision: the owner asked for 0.01 mi steps 10-20x a
    second, which the physics does not allow. At a real charge rate a
    hundredth of a mile takes seconds, so the display shows thousandths."""
    rate, _ = green.accrual_mi_per_s(2430.0, 3.5, 38, 107.8, 100.0)
    assert 0.01 / rate > 3.0, "0.01 mi must take multiple seconds at 2.4 kW"
    full, _ = green.accrual_mi_per_s(11300.0, 3.5, 38, 107.8, 100.0)
    assert 0.01 / full > 0.5, "even at full rate it is not 10-20 per second"
    assert 0.001 / full < 0.15, "thousandths, though, move fast enough to animate"


def test_the_grid_half_is_the_complement_of_the_solar_half():
    """Defined as a complement so solar + grid is exactly the draw, and the
    two can never disagree about the same tick."""
    for car_w, grid_w in ((2000, 100), (2000, -500), (2000, 0),
                          (2000, 5000), (0, 100), (1205, 900)):
        s = green.tick_solar_w(car_w, grid_w)
        g = green.tick_grid_w(car_w, grid_w)
        assert s + g == pytest.approx(max(0.0, car_w)), (car_w, grid_w)
        assert s >= 0 and g >= 0


def test_the_owners_case_two_kilowatts_against_nineteen_hundred():
    """The exact example that prompted this: 2 kW drawn, 1.9 kW of surplus,
    so 100 W is utility. The ledger used to call all 2,000 W solar."""
    car_w, grid_w = 2000.0, 100.0        # importing 100 W
    assert green.tick_solar_w(car_w, grid_w) == 1900.0
    assert green.tick_grid_w(car_w, grid_w) == 100.0
    assert green.tick_solar_fraction(car_w, grid_w) == pytest.approx(0.95)


def test_solar_fraction_is_bounded_and_safe_on_a_dead_car():
    assert green.tick_solar_fraction(0.0, -5000.0) == 0.0
    assert green.tick_solar_fraction(2000.0, -5000.0) == 1.0    # exporting
    assert green.tick_solar_fraction(2000.0, 9000.0) == 0.0     # deep import
    assert 0.0 <= green.tick_solar_fraction(1205.0, 600.0) <= 1.0


def test_ledger_banks_only_the_solar_proportion_of_a_rise():
    """The correction itself. A 10-point rise that was 95% solar banks 9.5
    points, not 10 -- and the old boolean call banked all 10."""
    banked, _ = green.ledger_step(0.0, 30, 40, 0.95, 0, 3600)
    assert banked == pytest.approx(9.5)

    banked, _ = green.ledger_step(0.0, 30, 40, 0.05, 0, 3600)
    assert banked == pytest.approx(0.5)


def test_the_fraction_reproduces_both_old_boolean_branches_exactly():
    """1.0 must behave as the old solar_charging=True branch and 0.0 as
    False, or this change silently rewrites every ordinary tick."""
    all_sun, _ = green.ledger_step(5.0, 30, 40, 1.0, 0, 3600)
    assert all_sun == pytest.approx(15.0), "the whole rise, as before"
    all_grid, _ = green.ledger_step(5.0, 30, 40, 0.0, 0, 3600)
    assert all_grid == pytest.approx(5.0), "unchanged; the rise dilutes it"


def test_a_partial_rise_still_respects_the_clamp():
    """The invariant 0 <= solar_soc <= soc_now must survive the new path."""
    banked, _ = green.ledger_step(0.0, 0, 5, 1.0, 0, 3600)
    assert 0 <= banked <= 5
    banked, _ = green.ledger_step(4.0, 5, 4, 0.9, 0, 3600)
    assert 0 <= banked <= 4


def test_lifetime_charged_is_derived_not_counted():
    """The defect the owner caught: "Charged so far" said 3.0 mi from sun
    while "Banked solar" said 10.4 free miles -- the pack cannot hold more
    sun than was ever put into it.

    Cause one was a stored counter that began at zero the day its column was
    added, so it silently excluded every tick before then. Deriving both
    figures from the same never-pruned tick log removes the failure mode
    rather than patching the number.
    """
    ticks = [
        {"state": "charging", "car_w": 2000, "grid_w": 100, "period_s": 3600},
        {"state": "charging", "car_w": 2000, "grid_w": -500, "period_s": 3600},
        {"state": "idle", "car_w": 0, "grid_w": -4000, "period_s": 3600},
    ]
    assert green.solar_kwh(ticks) == pytest.approx(1.9 + 2.0)
    assert green.grid_kwh(ticks) == pytest.approx(0.1 + 0.0)
    # Every engaged watt-hour lands in exactly one bucket.
    assert green.solar_kwh(ticks) + green.grid_kwh(ticks) == pytest.approx(4.0)


def test_idle_ticks_contribute_to_neither_half():
    """A car drawing nothing while the sun blazes charges nothing, and an
    unengaged tick is not the controller's to claim either way."""
    ticks = [{"state": "idle", "car_w": 5000, "grid_w": -9000, "period_s": 3600}]
    assert green.solar_kwh(ticks) == 0.0
    assert green.grid_kwh(ticks) == 0.0


def test_the_two_miles_figures_use_different_bases_and_must_not_be_mixed():
    """Why "charged so far" stops at kWh until mi/kWh is measured.

    banked_miles_rated needs NO pack size -- it is a fraction of the car's own
    reported range. kWh -> miles must go through mi/kWh = rated_range / pack,
    and with pack unknown that is a guess. On this car the ledger implied
    ~69 kWh against an assumed 100, so the same energy read 7.2 miles in one
    line and 10.4 in the other.
    """
    soc, range_mi = 40, 139.08
    banked = green.banked_miles_rated(3.0, soc, range_mi)
    assert banked == pytest.approx(10.43, abs=0.01)

    # The same 3 points of SoC, converted through an assumed 100 kWh pack.
    mpk_assumed, basis = green.effective_mi_per_kwh(None, soc, range_mi, None)
    assert basis == "rated"
    via_energy = 3.0 / 100 * green.NOMINAL_PACK_KWH * mpk_assumed
    assert via_energy == pytest.approx(10.43, abs=0.01), (
        "consistent ONLY when the pack really is NOMINAL_PACK_KWH")

    # With the pack the data actually implies, the same energy is fewer miles
    # -- which is precisely the contradiction the card was showing.
    mpk_real, _ = green.effective_mi_per_kwh(None, soc, range_mi, 68.8)
    assert 2.065 * mpk_real == pytest.approx(banked, abs=0.05)
    assert 2.065 * mpk_assumed < banked * 0.8, "the assumed pack understates it badly"


def test_charged_split_counts_manual_grid_charging_too():
    """The flattering-percentage bug. solar_kwh's ENGAGED_STATES filter drops
    charges the owner started themselves, which are still very much in the
    pack -- and which the banked ledger already counts. Leaving them out of
    the denominator read 61% solar on this site against a true 29%.
    """
    ticks = [
        # Controller driving a solar charge.
        {"state": "charging", "car_w": 2000, "grid_w": -1000, "period_s": 3600},
        # Owner charging at full rate from the grid; controller idle.
        {"state": "idle", "car_w": 11000, "grid_w": 12000, "period_s": 3600},
    ]
    solar, grid = green.charged_split(ticks)
    assert solar == pytest.approx(2.0)
    assert grid == pytest.approx(11.0), "the manual session must be counted"
    assert 100 * solar / (solar + grid) == pytest.approx(15.4, abs=0.1)

    # solar_kwh keeps its narrower, controller-scoped meaning on purpose.
    assert green.solar_kwh(ticks) == pytest.approx(2.0)


def test_charged_split_ignores_ticks_where_the_car_drew_nothing():
    assert green.charged_split(
        [{"state": "idle", "car_w": 0, "grid_w": 5000, "period_s": 3600}]) == (0.0, 0.0)


# --------------------------------------------------------------------------
# interval_solar_fraction: the banked ledger's attribution, integrated over a
# whole SoC step instead of sampled at the instant the step happened to land.
#
# Observed live 2026-09-13. An 81.6 kWh pack gains 1% in ~13 minutes at
# 3.6 kW, against a 120 s tick -- so a percentage point is roughly seven
# ticks wide, and ledger_step was crediting all of it from whichever single
# tick happened to cross the integer boundary:
#
#   12:17:27  grid -385 W (export)  car 3630 W  fraction 1.0   soc 84
#   12:19:28  grid +3996 W (import) car 3856 W  fraction 0.0   soc 84 -> 85
#
# A cloud at 12:19 booked the entire point as grid, though half of it went in
# under full sun two minutes earlier. On a partly cloudy day that is close to
# a coin flip, which is why banked_pct sat at 1.0% against 48.79 kWh of
# lifetime solar actually delivered.
# --------------------------------------------------------------------------

def _t(car_w, grid_w, period_s=120, state="charging"):
    return {"state": state, "car_w": car_w, "grid_w": grid_w,
            "period_s": period_s}


def test_interval_fraction_of_nothing_is_zero():
    """No ticks, or ticks where the car drew nothing, bank nothing -- rather
    than dividing by zero or guessing a half."""
    assert green.interval_solar_fraction([]) == 0.0
    assert green.interval_solar_fraction([_t(0, -4000)]) == 0.0


def test_interval_fraction_of_a_single_tick_matches_the_instant_formula():
    """One tick wide, this must agree with tick_solar_fraction exactly, or
    the fix would quietly change the easy case as well as the hard one."""
    for car_w, grid_w in ((3630, -385), (3856, 3996), (2000, 1000)):
        assert green.interval_solar_fraction([_t(car_w, grid_w)]) == pytest.approx(
            green.tick_solar_fraction(car_w, grid_w))


def test_the_regression_a_cloud_at_the_boundary_no_longer_erases_the_step():
    """The live 2026-09-13 pair. Sampling the last tick alone gives 0.0;
    integrating over both gives roughly half, which is what actually went in."""
    ticks = [_t(3630, -385), _t(3856, 3996)]
    assert green.tick_solar_fraction(3856, 3996) == 0.0      # what it used to see
    assert green.interval_solar_fraction(ticks) == pytest.approx(0.485, abs=0.01)


def test_interval_fraction_weights_by_energy_not_by_tick_count():
    """A long sunny tick outweighs a short cloudy one. Counting ticks rather
    than watt-hours would call this an even split."""
    ticks = [_t(4000, -4000, period_s=600), _t(4000, 4000, period_s=120)]
    assert green.interval_solar_fraction(ticks) == pytest.approx(600 / 720, abs=0.01)


def test_interval_fraction_is_bounded_to_the_unit_interval():
    assert green.interval_solar_fraction([_t(4000, -9000)]) == 1.0
    assert green.interval_solar_fraction([_t(4000, 9000)]) == 0.0


def test_interval_fraction_counts_charges_the_controller_did_not_drive():
    """Same reasoning as charged_split: a session the owner started at full
    rate from the grid still dilutes the bank, so it must not be filtered out
    of the denominator the way solar_kwh filters it out of a total."""
    ticks = [_t(7000, 7000, state="idle")]
    assert green.interval_solar_fraction(ticks) == 0.0


def test_the_errand_the_owner_described():
    """2026-09-19, in the owner's own terms. A pack holding 25 miles of
    banked sun goes out, drives 10 miles, and comes home: 15 miles of banked
    sun are left, and all 10 of those miles are counted as driven free.

    The pack here is 3.5 mi/kWh across 80 kWh, so a SoC point is 2.8 miles:
    25 banked miles is 8.93 points, and 10 miles is 3.57 of them.
    """
    pack_kwh, mi_per_kwh = 80.0, 3.5
    per_point = pack_kwh * mi_per_kwh / 100          # 2.8 miles per SoC point
    banked_points = 25.0 / per_point
    driven_points = 10.0 / per_point
    soc_before = 70
    soc_now = round(soc_before - driven_points)

    assert green.banked_miles_measured(
        banked_points, pack_kwh, mi_per_kwh) == pytest.approx(25.0)

    after, _ = green.ledger_step(banked_points, soc_before, soc_now, 0.0, 60, THRESH)
    left = green.banked_miles_measured(after, pack_kwh, mi_per_kwh)
    # SoC is a whole number, so a 3.57-point errand reads as 4 points: the
    # bank can only ever be as fine as half a point, 1.4 miles on this pack.
    assert left == pytest.approx(25.0 - 10.0, abs=per_point / 2), (
        "banked sun must be spent on the miles just driven")

    free, tracked, _ = green.free_miles_step(
        0.0, 0.0, 1000.0, 1010.0, banked_points, soc_before, soc_now)
    assert free == pytest.approx(10.0), "and every one of them was sun"
    assert tracked == pytest.approx(10.0)
