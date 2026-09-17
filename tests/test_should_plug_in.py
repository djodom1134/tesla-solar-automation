from __future__ import annotations

import solar

TUN = solar.Tunables()          # min_a 5, volts 240, margin_w 100 -> 1300 W

BASE = dict(plugged=False, location="home", soc=60, ceiling=95,
            surplus_w=4000.0, tun=TUN)


def d(**over):
    kw = dict(BASE); kw.update(over)
    return solar.should_plug_in(**kw)


def test_reminds_when_the_sun_is_up_and_the_car_is_home_with_room():
    assert d() is True


def test_never_reminds_about_a_car_already_plugged_in():
    """The whole point is the cable. A plugged-in car that is not charging is
    the controller's problem, not the owner's."""
    assert d(plugged=True) is False


def test_never_reminds_about_a_car_that_is_not_here():
    """Three-valued, same as everywhere else: away and unknown both refuse,
    because Tesla omits location keys rather than nulling them and a missing
    key is indistinguishable from a revoked scope."""
    assert d(location="away") is False
    assert d(location="unknown") is False


def test_never_reminds_when_there_is_nowhere_to_put_the_energy():
    """At or above the ceiling the controller would have nothing to do even
    with the cable in."""
    assert d(soc=95) is False
    assert d(soc=99) is False


def test_reminds_right_up_to_the_ceiling():
    assert d(soc=94) is True


def test_the_bar_is_the_controllers_own_start_threshold():
    """Not a magic number: start_watts is what the state machine itself
    requires before it will start a charge, so anything less would be a
    reminder to plug in for a charge that would never start."""
    bar = solar.start_watts(TUN)
    assert d(surplus_w=bar) is True
    assert d(surplus_w=bar - 1) is False


def test_the_bar_moves_with_the_tunables_rather_than_drifting():
    """A template hardcoding 1300 would silently go wrong the day min_a or
    margin_w changed. This must track them."""
    tun = solar.Tunables(min_a=10, margin_w=500)      # -> 2900 W
    assert d(surplus_w=2900.0, tun=tun) is True
    assert d(surplus_w=2899.0, tun=tun) is False


def test_unknown_never_guesses():
    assert d(soc=None) is False
    assert d(surplus_w=None) is False


def test_importing_is_never_a_reason_to_plug_in():
    assert d(surplus_w=-2000.0) is False
    assert d(surplus_w=0.0) is False
