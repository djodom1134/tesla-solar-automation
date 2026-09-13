from __future__ import annotations

import solar

BASE = dict(enabled=True, state="charging", soc=79, limit=80, ceiling=90,
            grid_w=-3000.0, raised_to=None, hold_elapsed_s=600,
            raise_hold_s=600, period_s=120, complete=False, plugged=True,
            location="home")


def d(**over):
    kw = dict(BASE); kw.update(over)
    return solar.raise_decision(**kw)


def test_raises_when_every_condition_holds():
    target, hold = d()
    assert target == 90


def test_never_raises_when_the_feature_is_off():
    assert d(enabled=False)[0] is None


def test_never_raises_outside_charging():
    for state in ("idle", "grace", "stopped"):
        assert d(state=state)[0] is None, state


def test_raises_at_most_once_per_engagement():
    """raised_to is the guard. Re-issuing set_charge_limit every tick would
    spend a billed command to assert a value the car already holds."""
    assert d(raised_to=90)[0] is None


def test_never_raises_speculatively():
    """Only while surplus is ACTUALLY being exported. grid_w >= 0 means the
    house is importing or balanced -- there is nothing to absorb."""
    assert d(grid_w=0.0)[0] is None
    assert d(grid_w=500.0)[0] is None


def test_does_not_raise_while_headroom_remains():
    """Below limit-2 there is still room to charge into; raising early would
    park the pack high for longer than necessary, which is what ages it."""
    assert d(soc=60)[0] is None


def test_unknown_soc_or_limit_never_raises():
    assert d(soc=None)[0] is None
    assert d(limit=None)[0] is None


def test_requires_the_hold_to_have_elapsed_on_a_previous_tick():
    """Same whole-tick semantics as start_hold_s: the timer is compared as
    carried in, so a period coarser than the hold still takes two ticks."""
    target, hold = d(hold_elapsed_s=0)
    assert target is None
    assert hold == 120


def test_the_timer_resets_when_a_condition_lapses():
    assert d(hold_elapsed_s=480, grid_w=+200.0)[1] == 0


def test_never_raises_below_or_equal_to_the_current_limit():
    assert d(ceiling=80)[0] is None
    assert d(ceiling=70)[0] is None


def test_the_ceiling_is_capped_at_one_hundred():
    assert d(ceiling=110, limit=95, soc=94)[0] == 100


# --- the "Complete" deadlock (observed live 2026-09-13 12:10) --------------
#
# A car sitting at 84% against an 85% limit reports charging_state
# "Complete", so charge_start comes back `car could not execute command:
# complete` and collector.py rolls the machine back to "stopped". The state
# gate above then refuses the raise -- and the raise is the only thing that
# would have given the car somewhere to put the sun. The limit stayed at 85
# from 8 to 13 September with the array exporting 4 kW.
#
# "Complete" is not "could not start yet" (a car still waking, which the
# rollback exists for). It is the car reporting that there is nothing left to
# charge INTO, which is a request for headroom, not a failure to act on.


def test_raises_when_the_car_stopped_because_it_hit_its_limit():
    assert d(state="stopped", complete=True)[0] == 90


def test_complete_bypasses_the_state_gate_from_any_state():
    for state in ("idle", "grace", "stopped"):
        assert d(state=state, complete=True)[0] == 90, state


def test_complete_never_bypasses_being_plugged_in_and_at_home():
    """The state gate used to carry this implicitly: advance() only reaches
    "charging" when the car is plugged in at home. Bypassing the state gate
    drops that guarantee, so it has to be asserted explicitly -- or a car
    finished charging somewhere else would have its limit raised from here."""
    assert d(state="stopped", complete=True, plugged=False)[0] is None
    assert d(state="stopped", complete=True, location="away")[0] is None
    assert d(state="stopped", complete=True, location="unknown")[0] is None


def test_an_ordinary_charge_still_requires_plugged_in_at_home():
    assert d(plugged=False)[0] is None
    assert d(location="away")[0] is None
    assert d(location="unknown")[0] is None


def test_complete_bypasses_only_the_state_gate_and_nothing_else():
    """Every other reason to refuse still refuses."""
    assert d(state="stopped", complete=True, enabled=False)[0] is None
    assert d(state="stopped", complete=True, raised_to=90)[0] is None
    assert d(state="stopped", complete=True, grid_w=+200.0)[0] is None
    assert d(state="stopped", complete=True, soc=60)[0] is None
    assert d(state="stopped", complete=True, soc=None)[0] is None
    assert d(state="stopped", complete=True, ceiling=80)[0] is None


def test_complete_still_waits_out_the_hold():
    """A raise is a billed command against an NCA pack. Completion is not a
    reason to skip the dwell that proves the export is real."""
    target, hold = d(state="stopped", complete=True, hold_elapsed_s=0)
    assert target is None
    assert hold == 120
