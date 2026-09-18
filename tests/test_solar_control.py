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


def test_watching_a_car_sleep_keeps_its_snapshot_actionable():
    """Observed live 2026-09-14: the car's last view was taken at 20:16, it
    slept, and at 02:16 -- SNAPSHOT_MAX_AGE_S to the minute -- the meter-only
    watch refused to look at the meter again. It stayed refused through a
    24.2 kWh solar day, 10.0 kWh of which went to the grid while the car sat
    plugged in at 84% against a 99% limit, and the controller logged no tick
    at all for the next seventeen hours.

    The gate could not do anything else. A sleeping car's snapshot is
    refreshed by exactly one thing -- a wake -- which is the thing the gate
    was blocking, so its age could only ever grow. Six hours after a car fell
    asleep the feature switched itself off and no amount of sunshine could
    switch it back on. The overnight case, which is every case that matters,
    was the one it could never serve.

    What the age was standing in for is "the car may have been driven away
    since". A car cannot be driven away while asleep -- moving it wakes it,
    and a wake is something the loop SEES on its next cheap state check. So
    an unbroken chain of observations that the car is still asleep is a
    stronger statement than any freshness cap, and it is free.
    """
    now = 1_760_000_000.0
    overnight = now - 17 * 3600          # last view seventeen hours ago
    one_poll_ago = now - 1800

    assert solar.asleep_confirmed(
        now=now, snapshot_ts=overnight, confirmed_ts=one_poll_ago,
        max_gap_s=3660) == now, (
        "an unbroken watch must carry the snapshot forward, however old it is")

    assert solar.knowledge_age_s(now, snapshot_ts=overnight,
                                 confirmed_ts=now) == 0.0


def test_a_gap_in_our_own_watching_falls_back_to_the_snapshot():
    """The chain is only as good as our own attendance. A collector that was
    restarted, or a Mac mini that suspended, did not see the car for that
    window -- and a car it did not see is a car that may have moved. Breaking
    the chain hands the question back to SNAPSHOT_MAX_AGE_S, which is the
    conservative answer it was always right about.
    """
    now = 1_760_000_000.0
    overnight = now - 17 * 3600

    assert solar.asleep_confirmed(
        now=now, snapshot_ts=overnight, confirmed_ts=now - 7200,
        max_gap_s=3660) is None, "two hours unwatched is not a chain"

    # And a broken chain means the age the cap actually judges is the
    # snapshot's own, so a seventeen-hour-old view is refused as before.
    assert solar.knowledge_age_s(now, snapshot_ts=overnight,
                                 confirmed_ts=None) == 17 * 3600
    assert not solar.sleeping_candidate(
        {"charging_state": "Stopped", "soc": 38, "limit": 91},
        age_s=17 * 3600, max_age_s=21600)


def test_the_chain_can_only_start_from_a_snapshot_still_in_its_own_cap():
    """First link. With nothing confirmed yet the snapshot has to vouch for
    itself, and it does that under the ordinary gap rule -- a car seen one
    poll ago is a car we are still watching; one last seen this morning is a
    chain we never had.
    """
    now = 1_760_000_000.0
    assert solar.asleep_confirmed(
        now=now, snapshot_ts=now - 1800, confirmed_ts=None,
        max_gap_s=3660) == now
    assert solar.asleep_confirmed(
        now=now, snapshot_ts=now - 6 * 3600, confirmed_ts=None,
        max_gap_s=3660) is None
    # Nothing to vouch for at all: a car this collector has never seen.
    assert solar.asleep_confirmed(
        now=now, snapshot_ts=None, confirmed_ts=None, max_gap_s=3660) is None


def _blind(**over):
    now = 1_760_000_000.0
    base = dict(enabled=True, running=True, capped=False,
                last_tick_ts=now - 17 * 3600, now=now, plugged=True,
                location="home", soc=84, limit=99, stale_after_s=3660)
    return solar.controller_blind(**{**base, **over})


def test_a_controller_that_has_stopped_looking_is_reported():
    """Nothing told the owner on 2026-09-14. The collector wrote its
    heartbeat on every one of the seventeen blind passes -- correctly, since
    the process was alive and the quiet paths are legitimate -- so
    collector_running said `true` all day and every alarm built on it stayed
    silent. The car sat plugged in with fifteen points of headroom under a
    24.2 kWh sky and the first anyone knew was the owner walking out to it.

    Liveness and usefulness are different questions. This one asks the
    second: there is a car to charge, there is a controller to charge it,
    and the controller has not looked at the meter in longer than any
    cadence it uses can explain.
    """
    assert _blind(), "seventeen hours without a tick must be reported"
    assert not _blind(last_tick_ts=1_760_000_000.0 - 1800), (
        "a tick one asleep-cadence ago is a working loop, not a blind one")
    # Never ticked at all, with every reason to have done so.
    assert _blind(last_tick_ts=None)


def test_the_legitimate_quiet_paths_are_not_mistaken_for_blindness():
    """Every one of these produces no ticks and SHOULD produce none. An
    alarm that fires on them is an alarm the owner learns to ignore, which
    is worse than no alarm at all -- it was silence that cost the day, but
    noise would have cost every day after it.
    """
    assert not _blind(enabled=False), "switched off is not blind"
    assert not _blind(running=False), "collector_offline owns a dead process"
    assert not _blind(capped=True), "api_cap_reached owns the daily cap"
    assert not _blind(plugged=False), "nothing to charge"
    assert not _blind(location="away"), "not our meter to watch"
    assert not _blind(location="unknown"), "unknown never accuses"
    # No headroom: sleeping_candidate refuses this car on purpose, so the
    # absence of ticks is the system working exactly as designed.
    assert not _blind(soc=98, limit=99)
    assert not _blind(soc=None)
    assert not _blind(limit=None)


# --- 2026-09-17: the watch believed a plug that Tesla kept denying ---------

def test_a_refusal_naming_disconnected_corrects_the_snapshot():
    """THE DAY THIS EXISTS FOR, 2026-09-17.

    The car was unplugged some time on the 15th while asleep. The meter-only
    watch reasoned from the snapshot taken before that, which says
    `plugged_in: true` and `charging_state: "Stopped"` at 39% against an 80%
    limit -- every clause sleeping_candidate asks for. So the watch held, and
    a held watch makes NO vehicle call, so nothing ever replaced the view
    that was wrong. Forty-four and a half hours, 122 requests spent, not one
    of them a look at the car.

    The unbroken-chain rule (asleep_confirmed) is what let the age grow that
    far: it vouches that a sleeping car has not MOVED, which is true and
    free, and knowledge_age_s therefore answered 0 s on every tick while the
    view underneath it aged out of all recognition. Location was never the
    thing that had gone stale. The PLUG was, and a human can pull a cable
    from a sleeping car without waking it, so no amount of attendance can
    vouch for that.

    What makes this recoverable rather than merely unlucky is that the car
    told us, nineteen times. Every surplus crossing sent charge_start and
    every one came back `car could not execute command: disconnected`. That
    refusal is ground truth about the exact field the snapshot had wrong, and
    it was logged and dropped on the floor while the next tick went back to
    believing the snapshot.

    So a refusal that contradicts the snapshot corrects it. Matched as a
    SUBSTRING for the reason _command already documents -- the signing proxy
    wraps the car's reason in prose, and the live string is "car could not
    execute command: disconnected".
    """
    correction = solar.snapshot_correction(
        "car could not execute command: disconnected")
    assert correction == {"charging_state": "Disconnected", "plugged_in": False}

    # And that correction is precisely what makes the watch let go: the
    # Disconnected clause sleeping_candidate already has does the rest, so
    # the very next tick pays for a real state check instead of a 45th hour.
    stale = {"charging_state": "Stopped", "soc": 39, "limit": 80,
             "plugged_in": True}
    assert solar.sleeping_candidate(stale, age_s=0.0, max_age_s=21600), (
        "precondition: this is the view that held the watch open")
    assert not solar.sleeping_candidate(
        {**stale, **correction}, age_s=0.0, max_age_s=21600)


def test_only_reasons_that_actually_contradict_the_snapshot_correct_it():
    """A refusal is not a licence to rewrite the view generally. `complete`
    and `is_charging` say something about the CHARGE, not about the cable,
    and a car that is plugged in and full is still plugged in -- correcting
    the plug there would disarm the watch on a car it should be watching.
    Unknowns and absences say nothing at all.
    """
    for reason in ("car could not execute command: complete",
                   "car could not execute command: is_charging",
                   "car could not execute command: not_charging",
                   "", None):
        assert solar.snapshot_correction(reason) is None, reason


def test_a_ticking_controller_can_still_be_blind_and_must_say_so():
    """controller_blind was built for 2026-09-14, when the loop logged no
    tick for seventeen hours, and it asks the only question that day needed:
    is anything still happening? On 2026-09-17 the answer was yes. Ticks
    landed every five minutes, solar_ticks was current to 135 s, and the
    alarm was correctly silent -- while the view underneath every one of
    those ticks was 44.5 hours old.

    Liveness, usefulness, and now FRESHNESS are three questions, and the
    second incident was the third one going unasked. A controller reasoning
    from a day-old view is not looking at the car, however busy it looks.

    Kept separate from controller_blind on purpose: that function reasons
    about the car (plugged, soc, limit) from the snapshot, and the whole
    claim here is that the snapshot cannot be trusted. An alarm about stale
    data must not be gated on the stale data.
    """
    day = 24 * 3600
    assert solar.knowledge_stale(running=True, snapshot_age_s=160438,
                                 max_age_s=day), "the live outage"
    assert not solar.knowledge_stale(running=True, snapshot_age_s=135,
                                     max_age_s=day)

    # An ordinary overnight sleep is exactly what the unbroken watch exists
    # to carry, so the bar has to sit well past one. An alarm that fires
    # every morning is one the owner learns to ignore, and that is how the
    # seventeen hours were missed in the first place.
    assert not solar.knowledge_stale(running=True, snapshot_age_s=17 * 3600,
                                     max_age_s=day)

    # A dead collector already has its own signal (collector_running), and
    # two alarms for one fault is the noise this file keeps arguing against.
    assert not solar.knowledge_stale(running=False, snapshot_age_s=160438,
                                     max_age_s=day)

    # A car this collector has never seen has no view to be stale, but it
    # equally has nothing to reason from -- which is the alarming case, not
    # the quiet one.
    assert solar.knowledge_stale(running=True, snapshot_age_s=None,
                                 max_age_s=day)


def test_a_car_at_its_limit_is_worth_watching_while_the_limit_may_rise():
    """Headroom the controller can make is headroom. A car at its limit is
    watched when a raise is still possible -- and only then."""
    full = {"charging_state": "Complete", "soc": 80, "limit": 80}
    assert not solar.sleeping_candidate(full, 600, 21600)
    assert solar.sleeping_candidate(full, 600, 21600, raise_to=90)
    # A raise to where the car already is makes no room.
    assert not solar.sleeping_candidate(full, 600, 21600, raise_to=80)
    assert not solar.sleeping_candidate({**full, "soc": 89}, 600, 21600,
                                        raise_to=90)
    # Never a reason to watch an unplugged or unplaceable car.
    assert not solar.sleeping_candidate(
        {**full, "charging_state": "Disconnected"}, 600, 21600, raise_to=90)
    assert not solar.sleeping_candidate(full, 21601, 21600, raise_to=90)


def test_raisable_to_is_the_ceiling_only_while_a_raise_is_still_allowed():
    conf = {"raise_limit": 1, "soc_ceiling": 90}
    assert solar.raisable_to(conf, {"raised_to": None}) == 90
    assert solar.raisable_to(conf, {"raised_to": 90}) is None
    assert solar.raisable_to({**conf, "raise_limit": 0},
                             {"raised_to": None}) is None


def test_at_limit_draws_the_same_line_as_sleeping_candidate():
    assert solar.at_limit({"soc": 79, "limit": 80})
    assert solar.at_limit({"soc": 80, "limit": 80})
    assert not solar.at_limit({"soc": 78, "limit": 80})
    assert not solar.at_limit({"soc": None, "limit": 80})
