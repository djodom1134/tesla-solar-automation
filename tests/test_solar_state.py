from __future__ import annotations

import solar

TUN = solar.Tunables()
POL = solar.Policy(grace_s=180, restart_hold_s=300, start_hold_s=60, enabled=True)
PERIOD = 120
START_W = TUN.min_a * TUN.volts + TUN.margin_w        # 1300


def tick(surplus_w, *, grid_w=None, current_a=5, location="home", plugged=True):
    if grid_w is None:
        grid_w = -surplus_w        # car off: surplus is pure export
    return solar.Tick(
        surplus_w=surplus_w,
        decision=solar.control(grid_w, current_a, TUN),
        location=location, plugged=plugged, period_s=PERIOD,
    )


def m(state, **kw):
    return solar.Machine(state=state, breach_ticks=kw.get("breach_ticks", 0),
                         recover_ticks=kw.get("recover_ticks", 0),
                         grace_s_elapsed=kw.get("grace_s_elapsed", 0),
                         hold_s=kw.get("hold_s", 0))


def test_idle_requires_sustained_surplus_before_charging():
    machine = m("idle")
    machine, actions = solar.advance(machine, tick(3000), POL, TUN)
    assert machine.state == "idle", "must hold for start_hold_s first"
    machine, actions = solar.advance(machine, tick(3000), POL, TUN)
    assert machine.state == "charging"
    assert "charge_start" in actions


def test_idle_ignores_surplus_below_the_floor():
    machine = m("idle", hold_s=999)
    machine, _ = solar.advance(machine, tick(START_W - 1), POL, TUN)
    assert machine.state == "idle"


def test_idle_does_nothing_when_not_plugged_in():
    machine = m("idle", hold_s=999)
    machine, actions = solar.advance(machine, tick(5000, plugged=False), POL, TUN)
    assert machine.state == "idle"
    assert actions == []


def test_charging_converged_does_not_fall_into_grace():
    """THE REGRESSION. A car charging perfectly at 40 A on 9.6 kW of sun has an
    error near zero. An earlier design compared that against a 1200 W floor and
    dropped to grace on every tick, stop/starting all afternoon."""
    machine = m("charging")
    for _ in range(10):
        machine, actions = solar.advance(
            machine, tick(9600, grid_w=0, current_a=40), POL, TUN)
        assert machine.state == "charging"
        assert "charge_stop" not in actions


def test_a_single_breach_tick_does_not_enter_grace():
    """grid_power refreshes at 60 s while the car's current comes from its own
    clock, so one tick after an amps write the two disagree."""
    machine = m("charging")
    machine, _ = solar.advance(machine, tick(800, grid_w=400, current_a=5), POL, TUN)
    assert machine.state == "charging"
    assert machine.breach_ticks == 1


def test_two_consecutive_breach_ticks_enter_grace():
    machine = m("charging", breach_ticks=1)
    machine, actions = solar.advance(machine, tick(800, grid_w=400, current_a=5), POL, TUN)
    assert machine.state == "grace"
    assert "set_amps" in actions          # snap straight to min_a


def test_grace_expiry_stops_and_restores():
    machine = m("grace", grace_s_elapsed=POL.grace_s)
    machine, actions = solar.advance(machine, tick(500, grid_w=700, current_a=5), POL, TUN)
    assert machine.state == "stopped"
    assert actions.index("charge_stop") < actions.index("restore")


def test_grace_recovery_needs_two_ticks_above_a_hysteresis_band():
    machine = m("grace", grace_s_elapsed=60)
    machine, _ = solar.advance(machine, tick(4000, grid_w=-2800, current_a=5), POL, TUN)
    assert machine.state == "grace", "one good tick is not enough"
    machine, _ = solar.advance(machine, tick(4000, grid_w=-2800, current_a=5), POL, TUN)
    assert machine.state == "charging"
    assert machine.grace_s_elapsed == 0, "timer must reset on recovery"


def test_grace_does_not_recover_inside_the_hysteresis_band():
    """Recovery needs raw_target >= min_a + 1, not merely >= min_a.

    That one amp is the whole anti-chatter margin: a surplus hovering exactly
    at the floor would otherwise flip grace<->charging on every tick. This
    lands raw_target at 5.5 -- above min_a, inside the band -- and must NOT
    recover, however many consecutive ticks it persists.

    Uses a policy with an effectively infinite grace_s. At the real
    grace_s=180 and period_s=120, the grace *timeout* alone (elapsed >
    grace_s) forces a transition to "stopped" by the second tick regardless
    of the recovery condition -- verified this fires even against the
    correct, unmutated code -- which would mask the exact property this test
    exists to isolate. Decoupling it is what lets the loop below prove the
    band holds for as many ticks as you care to run it.
    """
    d = solar.control(-220, 5, TUN)
    assert TUN.min_a <= d.raw_target < TUN.min_a + 1, "premise of this test changed"
    t = solar.Tick(surplus_w=1420, decision=d, location="home",
                   plugged=True, period_s=PERIOD)
    no_timeout = solar.Policy(grace_s=10_000, restart_hold_s=POL.restart_hold_s,
                              start_hold_s=POL.start_hold_s, enabled=True)
    machine = m("grace", grace_s_elapsed=60)
    for _ in range(3):
        machine, actions = solar.advance(machine, t, no_timeout, TUN)
        assert machine.state == "grace"
        assert actions == []


def test_stopped_requires_a_long_hold_before_spending_a_wake():
    machine = m("stopped")
    machine, actions = solar.advance(machine, tick(5000), POL, TUN)
    assert machine.state == "stopped"
    assert "wake" not in actions
    machine = m("stopped", hold_s=POL.restart_hold_s)
    machine, actions = solar.advance(machine, tick(5000), POL, TUN)
    assert machine.state == "charging"
    assert actions.index("wake") < actions.index("charge_start")


def test_stopped_hold_resets_when_surplus_drops():
    machine = m("stopped", hold_s=240)
    machine, _ = solar.advance(machine, tick(200), POL, TUN)
    assert machine.hold_s == 0


def test_unplugging_restores_and_idles_from_any_state():
    for state in ("charging", "grace", "stopped"):
        machine, actions = solar.advance(m(state), tick(3000, plugged=False), POL, TUN)
        assert machine.state == "idle", state
        assert "restore" in actions, state


def test_driving_away_restores_and_idles():
    machine, actions = solar.advance(m("charging"), tick(3000, location="away"), POL, TUN)
    assert machine.state == "idle"
    assert "restore" in actions


def test_unknown_location_freezes_and_issues_nothing():
    """Restoring is itself a command. Not knowing where the car is is not
    grounds to send one."""
    for state in ("charging", "grace", "stopped"):
        machine, actions = solar.advance(m(state), tick(3000, location="unknown"), POL, TUN)
        assert machine.state == state, state
        assert actions == [], state


def test_disabling_restores_and_idles():
    off = solar.Policy(grace_s=180, restart_hold_s=300, start_hold_s=60, enabled=False)
    machine, actions = solar.advance(m("charging"), tick(3000), off, TUN)
    assert machine.state == "idle"
    assert "restore" in actions


def test_every_state_is_declared():
    assert solar.STATES == {"idle", "charging", "grace", "stopped"}


def test_no_set_amps_action_inside_the_deadband_even_when_target_differs():
    """write=False does not imply target_a == current_a: a small error still
    rounds to a 1 A step. If advance() emitted set_amps regardless of `write`,
    the car would be nudged every tick inside the deadband -- precisely the
    hunting the deadband exists to prevent."""
    d = solar.control(-300, 20, TUN)          # error_w=200, inside deadband
    assert d.write is False and d.target_a != 20, "premise of this test changed"
    t = solar.Tick(surplus_w=5000, decision=d, location="home",
                   plugged=True, period_s=PERIOD)
    machine, actions = solar.advance(m("charging"), t, POL, TUN)
    assert actions == []
    assert machine.state == "charging"
