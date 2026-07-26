from __future__ import annotations

import solar

BASE = dict(enabled=True, state="charging", soc=79, limit=80, ceiling=90,
            grid_w=-3000.0, raised_to=None, hold_elapsed_s=600,
            raise_hold_s=600, period_s=120)


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
