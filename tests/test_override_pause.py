"""The manual-override pause: an owner who moves the charge-rate slider in
the Tesla app, or on the car's own screen, takes the car back from the solar
controller until charging is stopped and started again.

Everything here is pure -- no clock, no database, no network -- for the same
reason advance() and force_plan() are: the whole hazard in this feature is
mistaking ordinary command-propagation lag for an owner's decision, and that
is a question about three values, not about a system.
"""
from __future__ import annotations

import solar


# --- override_step: the acknowledge-then-diverge handshake ------------------
#
# The naive test -- "the car reports a rate different from the one we wrote"
# -- is routinely true right after a write and would disable automation for
# the rest of the session over nothing. Latching requires the car to have
# ADOPTED our value and then moved away from it.

def test_a_write_the_car_has_not_reported_yet_is_not_an_override():
    """The tick right after set_charging_amps: the car is still reporting the
    old rate because our command has not reached vehicle_data yet. This is
    the false positive the whole handshake exists to prevent."""
    ack, override = solar.override_step(
        commanded_amps=12, commanded_ack=False, charge_amps=48)
    assert ack is False
    assert override is None


def test_the_car_reporting_our_value_acknowledges_it():
    ack, override = solar.override_step(
        commanded_amps=12, commanded_ack=False, charge_amps=12)
    assert ack is True
    assert override is None


def test_diverging_after_acknowledgement_is_an_override():
    """The car said 12, then said 32. Nothing but an outside writer produces
    that, so the latch carries the owner's own number."""
    ack, override = solar.override_step(
        commanded_amps=12, commanded_ack=True, charge_amps=32)
    assert override == 32
    assert ack is True


def test_holding_our_value_after_acknowledgement_stays_quiet():
    ack, override = solar.override_step(
        commanded_amps=12, commanded_ack=True, charge_amps=12)
    assert ack is True
    assert override is None


def test_a_controller_rewrite_resets_the_handshake():
    """Caller sets commanded_amps=16 and ack=False on its own write. The tick
    where the car still reports the PREVIOUS value must not latch -- which is
    exactly the first case above, restated at the point it actually bites."""
    ack, override = solar.override_step(
        commanded_amps=16, commanded_ack=False, charge_amps=12)
    assert ack is False
    assert override is None


def test_nothing_commanded_can_never_be_overridden():
    """A rate change while the controller is idle is the owner setting their
    own car, not taking it from anyone. With no command outstanding there is
    no handshake to break."""
    ack, override = solar.override_step(
        commanded_amps=None, commanded_ack=False, charge_amps=32)
    assert ack is False
    assert override is None


def test_an_unknown_rate_is_inconclusive_not_an_override():
    """charge_current_request absent from the view. Absence is not evidence;
    guessing here would disable automation on a partial payload."""
    ack, override = solar.override_step(
        commanded_amps=12, commanded_ack=True, charge_amps=None)
    assert ack is True
    assert override is None


# --- override_cleared: stop, then start ------------------------------------

def test_charging_straight_through_does_not_clear_the_pause():
    armed, cleared = solar.override_cleared(False, "Charging")
    assert (armed, cleared) == (False, False)


def test_charging_stopping_arms_the_resume():
    armed, cleared = solar.override_cleared(False, "Stopped")
    assert (armed, cleared) == (True, False)


def test_charge_complete_arms_the_resume():
    """Reaching the limit is a stop like any other -- the owner starting the
    car charging again afterwards is the gesture that resumes control."""
    armed, _ = solar.override_cleared(False, "Complete")
    assert armed is True


def test_unplugging_arms_the_resume():
    armed, _ = solar.override_cleared(False, "Disconnected")
    assert armed is True


def test_starting_again_once_armed_clears_the_pause():
    armed, cleared = solar.override_cleared(True, "Charging")
    assert cleared is True
    assert armed is False


def test_starting_counts_as_charging_for_the_resume():
    _, cleared = solar.override_cleared(True, "Starting")
    assert cleared is True


def test_staying_stopped_while_armed_stays_armed():
    armed, cleared = solar.override_cleared(True, "Stopped")
    assert (armed, cleared) == (True, False)


def test_an_unknown_charging_state_arms_rather_than_resuming():
    """A view without charging_state is not evidence the car is charging.
    Arming is the safe direction: it can only delay a resume, never cause a
    controller to grab a car the owner is still driving manually."""
    armed, cleared = solar.override_cleared(False, None)
    assert (armed, cleared) == (True, False)


# --- charge_mode: the fourth value -----------------------------------------

PAUSED = {"override_amps": 32}
CLEAR: dict = {"override_amps": None}


def test_an_override_shows_as_manual():
    conf = {"enabled": 1, "force_charge_until": None}
    assert solar.charge_mode(conf, PAUSED, 1000) == "manual"


def test_without_an_override_an_enabled_controller_is_still_solar():
    conf = {"enabled": 1, "force_charge_until": None}
    assert solar.charge_mode(conf, CLEAR, 1000) == "solar"


def test_a_live_force_outranks_a_pause():
    """Forcing is the owner taking control back explicitly, and PUT
    /charge-mode clears the latch on its way past."""
    conf = {"enabled": 1, "force_charge_until": 2000}
    assert solar.charge_mode(conf, PAUSED, 1000) == "now"


def test_off_outranks_a_pause():
    """The feature is switched off. That the owner also once moved a slider
    is not the thing worth reporting."""
    conf = {"enabled": 0, "force_charge_until": None}
    assert solar.charge_mode(conf, PAUSED, 1000) == "off"


def test_state_may_be_omitted_entirely():
    """Callers with no per-vehicle state -- a fresh install, or a reader that
    only has the config -- must still get a mode rather than a KeyError."""
    conf = {"enabled": 1, "force_charge_until": None}
    assert solar.charge_mode(conf, None, 1000) == "solar"
