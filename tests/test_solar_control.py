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


def test_unramped_target_a_skips_the_per_tick_ramp_but_stays_clamped():
    """Task 21: unramped_target_a is what the law wants RIGHT NOW, with no
    regard for ramp_a -- collector.py's adoption-tick bypass uses it only for
    a DOWNWARD move. A car adopted at 48 A with a deep grid import wants far
    below min_a; target_a is ramp-limited to one step (48 -> 40), but
    unramped_target_a jumps straight to the clamped floor (5)."""
    d = solar.control(16760, 48, T)         # the observed live 12:29 reading
    assert d.target_a == 40, "ramp-limited: one step down from 48"
    assert d.unramped_target_a == T.min_a, "unramped: straight to the floor"


def test_unramped_target_a_is_still_clamped_to_max_a():
    d = solar.control(-20000, 45, T)        # huge export, would want far above max
    assert d.unramped_target_a == T.max_a


def test_grace_rides_out_a_compressor_cycle_instead_of_stopping():
    """T1.1. The largest software win in the whole-house spec, and it needs
    no thermostat.

    Measured on 2026-07-27: the AC pushed the site into import, the machine
    burned its 180 s grace and issued charge_stop at 12:58. Two minutes
    later the compressor stopped and 4.7 kW began exporting -- but
    restart_hold_s kept the car at 0 A until 13:06. Six minutes of surplus
    thrown at the grid because a ~6 minute compressor cycle outlasted a
    3 minute grace.

    Holding at min_a through that cycle costs ~1.2 kW of import for its
    duration -- 0.12 kWh -- and saves ~0.42 kWh of export otherwise missed
    during the restart lockout. Roughly 3.5:1.

    Time alone cannot express that trade: it charges the same for a 100 W
    dip and an 1100 W one. Energy alone is unbounded against a compressor
    that runs for hours. So grace is bounded by BOTH, and the energy budget
    is what does the work.
    """
    tun = solar.Tunables(volts=240, min_a=5, max_a=48, ramp_a=8,
                         margin_w=100, deadband_w=250)
    pol = solar.Policy(grace_s=900, grace_budget_wh=150.0)

    # A compressor-sized import: the car sits at its 1.2 kW floor and every
    # watt of it is coming from the grid.
    grid_w = 4000.0
    d = solar.control(grid_w, tun.min_a, tun)
    assert d.floor_breach, "premise: this tick is below the floor"
    tick = solar.Tick(surplus_w=tun.min_a * tun.volts - grid_w, decision=d,
                      location="home", plugged=True, car_charging=True,
                      period_s=120)

    m = solar.Machine(state="grace")
    held = 0
    for _ in range(10):
        m, actions = solar.advance(m, tick, pol, tun)
        if m.state == "stopped":
            break
        assert m.state == "grace"
        held += 1

    assert m.state == "stopped", "the budget must eventually give up"
    # 150 Wh at 1,200 W is 450 s = 3.75 ticks of 120 s.
    assert 3 <= held <= 5, (
        f"held {held} ticks ({held * 120} s); expected ~450 s of ride-through "
        "from a 150 Wh budget at the 1.2 kW floor")
    assert held * 120 > 180, (
        "REGRESSION: gave up inside the old 180 s grace_s, which is shorter "
        "than a compressor cycle -- the exact behaviour that exported 4.7 kW "
        "on 2026-07-27")


def test_grace_budget_lasts_far_longer_when_the_import_is_small():
    """The discriminating half: a 150 W nuisance import is nearly free to
    ride out, and a time-only grace would abandon it just as fast as a
    4 kW one. Same policy, same floor, two very different disturbances.
    """
    tun = solar.Tunables(volts=240, min_a=5, max_a=48, ramp_a=8,
                         margin_w=100, deadband_w=250)
    pol = solar.Policy(grace_s=900, grace_budget_wh=150.0)

    d = solar.control(150.0, tun.min_a, tun)
    tick = solar.Tick(surplus_w=tun.min_a * tun.volts - 150.0, decision=d,
                      location="home", plugged=True, car_charging=True,
                      period_s=120)
    m = solar.Machine(state="grace")
    held = 0
    while held < 20:
        m, _ = solar.advance(m, tick, pol, tun)
        if m.state != "grace":
            break
        held += 1

    # Only 150 W of the car's 1,200 W is actually imported, so the budget
    # drains 8x slower: 5 Wh per tick against a 150 Wh budget. grace_s, not
    # the budget, is what finally ends this one -- which is the point.
    # (Timers are compared as carried in, so the machine holds seven whole
    # 120 s ticks and gives up on the eighth, when elapsed reaches 960.)
    assert held == 7, (
        f"held {held} ticks ({held * 120} s) for a 150 W import; expected the "
        "900 s time cap to bind, not the energy budget")
    assert m.grace_wh < pol.grace_budget_wh, (
        f"spent {m.grace_wh:.0f} Wh of a {pol.grace_budget_wh:.0f} Wh budget -- "
        "a cheap disturbance must be ended by time, not by energy")


def test_a_failed_recovery_attempt_does_not_refund_the_grace_budget():
    """A flickering surplus must not buy unlimited ride-through.

    The recovery branch carries grace_s_elapsed forward but originally
    dropped the energy accumulator, which reset it to zero. A surplus
    oscillating either side of the floor -- precisely what a cycling
    compressor produces -- would then refund the budget on every flicker
    and the car could import at the floor forever without the machine ever
    reaching its limit.
    """
    tun = solar.Tunables(volts=240, min_a=5, max_a=48, ramp_a=8,
                         margin_w=100, deadband_w=250)
    pol = solar.Policy(grace_s=100_000, grace_budget_wh=150.0)  # time can never bind

    def tick_at(grid_w):
        d = solar.control(grid_w, tun.min_a, tun)
        return solar.Tick(surplus_w=tun.min_a * tun.volts - grid_w, decision=d,
                          location="home", plugged=True, car_charging=True,
                          period_s=120)

    breaching = tick_at(4000.0)          # below the floor: spends budget
    flickering = tick_at(-1500.0)        # briefly above it: one recovery tick
    assert breaching.decision.floor_breach
    assert flickering.decision.raw_target >= tun.min_a + 1

    m = solar.Machine(state="grace")
    for _ in range(40):
        m, _ = solar.advance(m, breaching, pol, tun)
        if m.state != "grace":
            break
        # One tick above the floor -- not two, so it never actually recovers.
        m, _ = solar.advance(m, flickering, pol, tun)
        if m.state != "grace":
            break

    assert m.state == "stopped", (
        "REGRESSION: an alternating surplus refunded the energy budget every "
        "flicker, so grace never expired and the car imported at the floor "
        "indefinitely")


def test_a_sleeping_car_is_worth_watching_only_when_it_could_actually_use_the_sun():
    """The gate that lets the loop run on a SLEEPING car's last-known state.

    Observed 2026-07-28: the car sat plugged in at 38% against a 91% limit
    from 06:55 while the sun came up, and the controller never saw it. The
    loop runs only when vehicle_data returns a view, and vehicle_data returns
    nothing for a sleeping car -- so reaching the state machine's `wake`
    action required a tick, a tick required a view, and a view required the
    car to be awake. 66 asleep ticks were logged with 5 solar ticks between
    them.

    Acting on a stale view is only defensible when the view still implies
    there is something to gain, so every clause here is a reason to spend
    $0.02 on a wake.
    """
    fresh = {"charging_state": "Stopped", "soc": 38, "limit": 91}
    assert solar.sleeping_candidate(fresh, age_s=600, max_age_s=21600)

    # Not plugged: waking buys nothing.
    assert not solar.sleeping_candidate(
        {**fresh, "charging_state": "Disconnected"}, 600, 21600)
    assert not solar.sleeping_candidate(
        {**fresh, "charging_state": None}, 600, 21600)

    # No headroom: already at its limit.
    assert not solar.sleeping_candidate({**fresh, "soc": 91}, 600, 21600)
    assert not solar.sleeping_candidate({**fresh, "soc": 90}, 600, 21600)

    # Stale beyond usefulness -- the car may have been driven away since.
    assert not solar.sleeping_candidate(fresh, age_s=21601, max_age_s=21600)

    # Missing data is never an invitation to command a car.
    assert not solar.sleeping_candidate(None, 600, 21600)
    assert not solar.sleeping_candidate({**fresh, "soc": None}, 600, 21600)
    assert not solar.sleeping_candidate({**fresh, "limit": None}, 600, 21600)
    assert not solar.sleeping_candidate(fresh, age_s=None, max_age_s=21600)


def test_darkness_is_measured_from_the_array_not_a_clock():
    """The watch polls the meter every 300 s. Left running overnight that
    spent ~33 ticks a night on this site -- about $1.98/month, a fifth of the
    owner's whole API credit -- asking whether the sun was up at 1 a.m.

    Derived from the site's own production rather than a sunrise table: no
    new dependency, no timezone arithmetic, and automatically right in
    December, during an eclipse, and under snow.
    """
    assert solar.is_dark([0.0, 0.0, 0.0])
    assert solar.is_dark([120.0, 40.0, 0.0]), "well below the car's 1.2 kW floor"

    # A cloudy afternoon is NOT darkness -- the controller must keep watching.
    assert not solar.is_dark([2400.0, 180.0, 90.0])
    assert not solar.is_dark([0.0, 0.0, 300.0])


def test_darkness_needs_several_consecutive_readings():
    """One zero at dusk, or a single missing sample, must not park the watch
    for the night."""
    assert not solar.is_dark([0.0])
    assert not solar.is_dark([0.0, 0.0])
    assert not solar.is_dark([0.0, 0.0, 4000.0])


def test_too_little_history_keeps_watching():
    """Fails OPEN. An unnecessary tick costs $0.002; sleeping through a sunny
    morning costs the whole feature."""
    assert not solar.is_dark([])
    assert not solar.is_dark([None, None, None])
