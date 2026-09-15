"""Closed-loop solar charge control.

The control law and the state machine are pure: they take numbers and return
numbers, with no I/O of their own. collector.py owns the network and drives
them tick by tick. That split is what makes a control system testable without
a car, a roof, or the weather.

The persistence functions below them are the deliberate exception: they are
where the control law's and state machine's numbers live between ticks --
config, machine state, tick history, and the request counter -- and they do
touch a database. Nothing above them in this file does.

THE CENTRAL IDEA. grid_power already contains the car's own draw, so the loop
never needs to know what the car is consuming in absolute terms. It servos the
meter: push amps up while the site exports, back off while it imports, and the
system converges on grid ~= -margin_w. House disturbances (an AC compressor
starting) are rejected as a matter of course.

Computing surplus as `solar_power - load_power` instead would create positive
feedback -- raise amps, load rises, apparent surplus collapses, controller
backs off, oscillate. And it is not available anyway: live_status.wall_connectors
is empty on this site, so there is no site-side measurement of the car.

TWO DISTINCT QUANTITIES, never conflated:
  error_w   -- signed control error, driven to ZERO. Used only by control().
  surplus_w -- absolute solar available to the car. Used by the state machine,
               the UI, and logging.
A converged loop holds error_w near zero while surplus_w may be 9600 W.
Comparing error_w against an absolute floor makes a healthy charge look like a
dead one.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# charger_voltage reads 2 (not 0) when idle, so power is only meaningful in
# these two states -- docs/tesla-field-reference.md:97.
LIVE_CHARGING_STATES = {"Charging", "Starting"}


@dataclass(frozen=True)
class Tunables:
    margin_w: int = 100      # bias toward exporting a trickle rather than importing
    deadband_w: int = 250    # ~= one amp step; the smallest that cannot oscillate
    ramp_a: int = 8          # max amps of change per tick
    min_a: int = 5           # the car's own UI floor; below this needs a double-send
    max_a: int = 48          # charge_current_request_max
    volts: int = 240


@dataclass(frozen=True)
class Decision:
    target_a: int            # what to command, clamped and integral
    write: bool              # False when inside the deadband
    floor_breach: bool       # the law wanted less than min_a
    error_w: float
    raw_target: float        # unclamped; exists only to answer floor_breach
    # Clamped to [min_a, max_a] but NOT ramp-limited -- the "where the loop
    # would put the car right now, with no regard for ramp_a" figure. Used
    # only by the adoption-tick bypass in collector.py: a downward move all
    # the way there is safe (it can only reduce draw), an upward one is not,
    # so the caller still compares it against current_a before trusting it.
    unramped_target_a: int


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def car_watts(view: dict, volts: int) -> float:
    """The car's present AC draw, or 0.

    Pinned to ACTUAL charging, never to the standing amps setting: in idle and
    stopped the car draws nothing, and adding a nonexistent 1.2-11.5 kW would
    inflate surplus and start a charge into surplus that is not there.
    """
    if view.get("charging_state") not in LIVE_CHARGING_STATES:
        return 0.0
    amps = view.get("amps_actual")
    if amps is None:
        return 0.0
    return float(amps) * volts


def surplus_watts(car_w: float, grid_w: float) -> float:
    """Absolute solar available to the car. grid_w > 0 is import."""
    return car_w - grid_w


# Spec 5: "grid_power stuck | same value > 5 consecutive ticks | hold amps,
# flag suspect". Six consecutive byte-identical readings (">5") is the trigger;
# named here so collector.py's history fetch and this check can never drift
# out of sync with each other.
GRID_STUCK_TICKS = 6


def grid_is_stuck(recent: list[float], threshold: int = GRID_STUCK_TICKS) -> bool:
    """True when the grid meter has reported the SAME value too many ticks running.

    The meter refreshes every 60 s against a 120 s loop, so one or two repeats
    are normal and expected. A long run of byte-identical readings is not: it
    means the gateway has frozen, and a frozen NEGATIVE reading is the dangerous
    one -- error_w stays positive, the controller ramps to max_a and holds, and
    nothing in the state machine can ever observe the floor breach that would
    stop it. It would draw full power from the grid all night while the meter
    insisted it was sunny.

    Compares exact equality deliberately: a live meter essentially never repeats
    a float bit-for-bit, and any tolerance would mask a genuinely frozen value
    that happens to sit near a real one.
    """
    if len(recent) < threshold:
        return False
    return all(v == recent[0] for v in recent[:threshold])


def control(grid_w: float, current_a: int, tun: Tunables) -> Decision:
    """One tick of the integral controller."""
    error_w = -grid_w - tun.margin_w
    raw_target = current_a + error_w / tun.volts
    step_a = int(round(_clamp(error_w / tun.volts, -tun.ramp_a, tun.ramp_a)))
    target_a = int(_clamp(current_a + step_a, tun.min_a, tun.max_a))
    unramped_target_a = int(_clamp(round(raw_target), tun.min_a, tun.max_a))
    return Decision(
        target_a=target_a,
        write=abs(error_w) >= tun.deadband_w,
        # Expressed in AMPS, not watts, so the floor derives from the measured
        # voltage instead of a hardcoded 1200 W -- and so it stays correct in
        # every state rather than only when the car is idle.
        floor_breach=raw_target < tun.min_a,
        error_w=error_w,
        raw_target=raw_target,
        unramped_target_a=unramped_target_a,
    )


def sleeping_candidate(view: dict | None, age_s: float | None,
                       max_age_s: float) -> bool:
    """Whether a SLEEPING car's last-known state justifies watching the meter
    on its behalf.

    The loop runs only when vehicle_data returns a view, and vehicle_data
    returns nothing for a sleeping car. That made the state machine's `wake`
    action unreachable: a tick needed a view, a view needed the car awake, and
    the car would not wake itself for sunshine. Observed 2026-07-28 -- the car
    sat plugged in at 38% against a 91% limit from 06:55 through the whole
    morning while the controller logged 66 asleep ticks and never looked at
    the meter once.

    The fix is to let the loop reason from the stored snapshot, which is only
    defensible while that snapshot still implies something to gain. Every
    clause below is a reason to spend $0.02 on a wake:

      plugged   -- waking an unplugged car buys nothing and cannot charge
      headroom  -- at or within a point of its limit there is nowhere to put
                   the energy
      fresh     -- a car we have not been watching may have been driven away
                   since; acting on it would wake a car somewhere else

    `age_s` is time since we last KNEW where the car was, which is not the
    same as the snapshot's age and must not be passed as one -- see
    asleep_confirmed and knowledge_age_s, and the seventeen hours of sunshine
    that distinction cost on 2026-09-14. An unbroken watch answers with this
    tick however old the view is; only a car nobody was watching is judged on
    the age of the view itself.

    Missing data is never an invitation to command a car, so every unknown
    returns False rather than being treated as permissive.
    """
    if view is None or age_s is None or age_s > max_age_s:
        return False
    if view.get("charging_state") in (None, "Disconnected"):
        return False
    soc, limit = view.get("soc"), view.get("limit")
    if soc is None or limit is None:
        return False
    return soc < limit - 1


def asleep_confirmed(*, now: float, snapshot_ts: float | None,
                     confirmed_ts: float | None, max_gap_s: float,
                     ) -> float | None:
    """The moment we last KNEW where a sleeping car was, carried forward by
    one more observation that it is still asleep. None when the chain is
    broken and only the snapshot's own age can answer.

    sleeping_candidate's freshness clause stands in for "the car may have
    been driven away since", and against a sleeping car it cannot do that
    job: the one thing that refreshes a snapshot is a wake, which is the
    thing the clause blocks, so its age only ever grows. Observed live
    2026-09-14 -- the last view was taken at 20:16, the watch died at 02:16
    exactly SNAPSHOT_MAX_AGE_S later, and the car then sat plugged in at 84%
    against a 99% limit through a 24.2 kWh solar day, 10.0 kWh of which was
    exported. Seventeen hours, no tick logged. Every overnight sleep disarmed
    the feature before sunrise; the case it was written for, a car asleep
    since breakfast, is the only one it could ever serve.

    Attendance is the better question, and it is free. A car cannot be driven
    away while asleep -- moving it wakes it, and the loop's cheap state check
    sees that on its next pass. So while our observations are UNBROKEN, "the
    car is still asleep" is a live statement about where it is now, not a
    stale one about where it was, and the snapshot may be believed however
    old it is.

    "Asleep" here means anything that is not online, which is the same test
    the meter-only watch itself uses. Tesla settles a parked car from asleep
    into offline after a while and the distinction says nothing about
    movement: the collector log for 2026-09-14 carries six `asleep` lines and
    199 `offline` ones for the same motionless car in the same garage.
    Narrowing this to the literal "asleep" would put that entire day back.

    Unbroken means the gap since the last observation is within max_gap_s --
    the caller's own cadence plus slack, the same shape ha_routes.py judges
    the heartbeat by. A collector restarted, a Mac mini suspended, or an
    outage of any kind leaves a hole we cannot vouch across, and the chain
    breaks rather than papering over it. Breaking hands the question back to
    SNAPSHOT_MAX_AGE_S, which was always the right conservative answer for a
    car nobody was watching.

    The first link is the snapshot itself, judged by the same gap rule: a car
    seen one poll ago is one we are still watching. A car never seen at all
    has nothing to vouch for it.
    """
    last = confirmed_ts if confirmed_ts is not None else snapshot_ts
    if last is None or now - last > max_gap_s:
        return None
    return now


def knowledge_age_s(now: float, snapshot_ts: float | None,
                    confirmed_ts: float | None) -> float | None:
    """How long since we last knew where this car was -- what
    sleeping_candidate's `age_s` actually means, and what its cap judges.

    An unbroken watch (see asleep_confirmed) answers with this tick, so the
    age is a poll interval at most. A broken one answers with the snapshot,
    which is the only thing left that knows anything.
    """
    if confirmed_ts is not None:
        return now - confirmed_ts
    return None if snapshot_ts is None else now - snapshot_ts


def backoff_seconds(consecutive_429s: int, period_s: int,
                    retry_after: float | None, cap_s: int = 1800) -> int:
    """How long to wait after a 429 before the next request.

    Rate limits are shared with every other app on the owner's Tesla account,
    and exceeding Tesla's limit disables the whole application rather than
    merely throttling it -- so backing off is protecting access, not politeness.

    Honours a server-supplied Retry-After when there is one; the server knows
    better than any heuristic. Otherwise doubles from the configured period and
    caps, so a sustained outage settles at a slow poll rather than compounding.
    """
    if retry_after is not None:
        return min(int(round(retry_after)), cap_s)
    return min(period_s * 2 ** max(consecutive_429s, 0), cap_s)


def raise_decision(*, enabled: bool, state: str, soc: int | None,
                   limit: int | None, ceiling: int, grid_w: float,
                   raised_to: int | None, hold_elapsed_s: int,
                   raise_hold_s: int, period_s: int, complete: bool,
                   plugged: bool, location: str) -> tuple[int | None, int]:
    """Whether to raise the charge limit, and the updated hold timer.

    Returns (limit_to_raise_to or None, new_hold_elapsed_s).

    Headroom, not surplus, is the binding constraint on this system: at an 80%
    limit with the car at 76% there are ~4 kWh of room against a median
    13.8 kWh/day of export, so without a raise the controller fills the pack in
    under two hours and the rest goes to the grid anyway.

    Every gate here exists for a reason:
      * ACTUALLY EXPORTING (grid_w < 0), never speculatively -- raising the
        limit on a forecast would park an NCA pack high for nothing.
      * NEAR THE LIMIT (soc >= limit - 2) -- while headroom remains there is
        somewhere to put the energy already, and time spent high is what ages
        the pack, not reaching high.
      * ONCE PER ENGAGEMENT (raised_to is None) -- re-issuing the command every
        tick would spend a billed write to assert a value the car holds.
      * UNKNOWN NEVER GUESSES -- a missing soc or limit returns None, matching
        the three-valued discipline used for location.
      * PLUGGED IN, AT HOME -- see `complete` below for why this is stated
        here rather than inherited from the state gate.

    `complete` opens the one door the state gate has to leave open. A car
    sitting just under its limit reports charging_state "Complete", so
    charge_start returns `car could not execute command: complete` and
    collector.py rolls the machine back to "stopped" -- a rollback that is
    right for its original case, a car still coming out of sleep. But then
    the state gate refuses the raise, and the raise is the only thing that
    would have given the car somewhere to put the sun. Observed live on
    2026-09-13: an 85% limit against 84% SoC held from 8 September while the
    array exported 4 kW, because every tick refused in exactly this loop.

    "Complete" is not "could not start yet". It is the car saying there is no
    headroom left, which is a request for headroom rather than a failure to
    act on -- so it bypasses the state gate and nothing else.

    Bypassing that gate costs the guarantee it used to carry for free:
    advance() only ever reaches "charging" with the car plugged in at home,
    so those two conditions never needed stating. They do now, or a car that
    finished charging somewhere else would have its limit raised from here.

    The hold is compared AS CARRIED IN, like start_hold_s, so the real wait is
    period_s * (ceil(raise_hold_s / period_s) + 1) -- never under two ticks.
    """
    if not enabled or raised_to is not None:
        return None, 0
    if not plugged or location != "home":
        return None, 0
    if state != "charging" and not complete:
        return None, 0
    if soc is None or limit is None:
        return None, 0
    if grid_w >= 0 or soc < limit - 2:
        return None, 0
    if hold_elapsed_s >= raise_hold_s:
        target = min(int(ceiling), 100)
        return (target if target > limit else None), hold_elapsed_s
    return None, hold_elapsed_s + period_s


def controller_blind(*, enabled: bool, running: bool, capped: bool,
                     last_tick_ts: float | None, now: float,
                     plugged: bool, location: str, soc: int | None,
                     limit: int | None, stale_after_s: float) -> bool:
    """Whether the controller has stopped looking at the meter while there
    was every reason to be watching it.

    Reported, never acted on -- ha_routes carries it to Home Assistant, which
    owns the notifying, exactly as should_plug_in is.

    THE DAY THIS EXISTS FOR, 2026-09-14. The meter-only watch disarmed itself
    at 02:16 (see asleep_confirmed) and the loop logged no tick for the next
    seventeen hours, through a 24.2 kWh solar day, with the car plugged in at
    84% against a 99% limit. Nothing anywhere said so. The heartbeat was
    written on every one of those passes -- correctly, because the process
    was alive and every quiet path it took is a legitimate one -- so
    collector_running answered `true` all day and every signal built on it
    agreed. The owner found out by walking out to the car.

    Liveness and usefulness are different questions, and only the first had
    an answer. This is the second: a car to charge, a controller to charge it
    with, and no tick in longer than any cadence that controller uses can
    explain.

    The exclusions are the design. Each names a condition that produces no
    ticks and SHOULD produce none, and each already has its own signal where
    the owner would want one -- a switched-off feature, a dead process
    (collector_running), a spent daily budget (capped). The one that is not
    an alarm at all is a car with no headroom: sleeping_candidate refuses to
    watch on its behalf deliberately, so silence there is the system working.
    An alarm that fires on any of these is one the owner learns to ignore,
    and silence cost a single day where noise would cost every day after it.

    stale_after_s belongs to the caller because only the caller knows the
    cadence actually in force -- 120 s engaged, 1800 s after dark.
    """
    if not enabled or not running or capped:
        return False
    if not plugged or location != "home":
        return False
    if soc is None or limit is None or soc >= limit - 1:
        return False
    if last_tick_ts is None:
        return True
    return (now - last_tick_ts) > stale_after_s


def should_plug_in(*, plugged: bool, location: str, soc: int | None,
                   ceiling: int, surplus_w: float | None,
                   tun: Tunables) -> bool:
    """Whether the owner is leaving sunshine on the table for want of a cable.

    Every other feature here can act on its own. This one cannot -- nothing
    in the Fleet API plugs a car in -- so it exists purely to be reported,
    and ha_routes carries it to Home Assistant, which owns the notifying.

    The bar is start_watts, the same threshold advance() itself requires
    before starting a charge, deliberately rather than a round number. A
    reminder that fires below it is a reminder to plug in for a charge that
    would never start; and stating it here rather than in an HA template
    means it keeps tracking min_a and margin_w instead of quietly going
    wrong the first time either changes.

    At or above the ceiling there is nowhere to put the energy, so there is
    nothing to be reminded about. Unknowns refuse rather than guess, and
    location is three-valued for the usual reason -- Tesla omits the keys
    rather than nulling them, so "away" and "we cannot tell" are different
    claims and neither is "home".
    """
    if plugged or location != "home":
        return False
    if soc is None or surplus_w is None:
        return False
    if soc >= ceiling:
        return False
    return surplus_w >= start_watts(tun)


STATES = frozenset({"idle", "charging", "grace", "stopped"})


@dataclass(frozen=True)
class Policy:
    # Grace is bounded by BOTH a time cap and an energy budget, and the
    # budget is what does the work. Time alone charges the same for a
    # 100 W dip as for an 1,100 W one; energy alone is unbounded against
    # a compressor that runs for hours. grace_s is deliberately generous
    # now -- it binds only when the disturbance is small enough that
    # riding it out is nearly free.
    grace_s: int = 900          # absolute cap on holding at min_a
    # Sized from the DISTURBANCE, not from a round number: a residential
    # compressor cycle runs 6-10 minutes, and the car spends its 1.2 kW
    # floor for the duration, so bridging one costs 120-200 Wh. 250 Wh
    # covers a 12.5 minute cycle with margin. An earlier 150 Wh gave up
    # one tick before the compressor stopped on 2026-07-27 and recovered
    # nothing -- the budget must span the whole cycle or it buys nothing
    # at all, since a partial ride-through still pays the restart lockout.
    grace_budget_wh: float = 250.0   # grid energy the car may spend riding out a dip
    # start_hold_s and restart_hold_s are enforced as WHOLE TICKS, not raw
    # seconds: the timer is compared as carried in from the previous tick, so
    # the real wait is period_s * (ceil(threshold / period_s) + 1) and is never
    # less than two ticks. At the default 120 s tick, start_hold_s=60 means a
    # 240 s wait, and restart_hold_s=300 means 480 s. At a 300 s tick they
    # become 600 s and 600 s. This is deliberate -- one grid-meter reading is
    # not evidence of a *sustained* condition, and both transitions issue
    # billed commands -- but the numbers are not the wait in seconds.
    restart_hold_s: int = 300   # sustained surplus before spending a wake
    start_hold_s: int = 60      # sustained surplus before starting from idle
    enabled: bool = True


@dataclass(frozen=True)
class Machine:
    state: str = "idle"
    breach_ticks: int = 0       # consecutive ticks below the floor
    recover_ticks: int = 0      # consecutive ticks back above it
    grace_s_elapsed: int = 0
    hold_s: int = 0             # sustained-surplus timer for idle and stopped
    # Grid energy the CAR itself has drawn while riding out a dip at the
    # floor. Grace is bounded by this as well as by time -- see advance().
    # Distinct from the module-level grace_import_wh(), which reports the
    # same quantity from logged history; this one is the live accumulator
    # the pure machine carries between ticks.
    grace_wh: float = 0.0


@dataclass(frozen=True)
class Tick:
    surplus_w: float
    decision: Decision
    location: str               # "home" | "away" | "unknown"
    plugged: bool
    period_s: int
    # True when the car is ALREADY drawing power we did not command -- it
    # auto-started on plug-in, or was started from the Tesla app. Defaults
    # False so every pre-existing call site (none of which knows about
    # adoption) keeps behaving exactly as before.
    car_charging: bool = False


def start_watts(tun: Tunables) -> float:
    """The absolute surplus needed to sustain the minimum charge rate."""
    return tun.min_a * tun.volts + tun.margin_w


def advance(m: Machine, t: Tick, pol: Policy, tun: Tunables) -> tuple[Machine, list[str]]:
    """One state transition. Returns the next machine and an ORDERED action list.

    Actions are names, not calls -- collector.py performs them. That keeps this
    function pure and lets the backtest run the whole machine with no network.
    """
    # Unknown location freezes everything. Restoring is itself a command, and
    # "we do not know where the car is" is not grounds to send one.
    if t.location == "unknown":
        return m, []

    if not pol.enabled or not t.plugged or t.location != "home":
        if m.state == "idle":
            return Machine(state="idle"), []
        return Machine(state="idle"), ["restore"]

    if m.state == "idle":
        if t.car_charging:
            # ADOPT: the car is already charging -- it auto-started on
            # plug-in, or the owner started it from the app. No new
            # `charge_start` (it would be commanding a charge that is
            # already running) and NO sustained hold: the hold exists so one
            # noisy meter reading cannot START a charge, but here the charge
            # is already running and the only question is who controls it.
            # Waiting two ticks just means two more ticks of grid import.
            return Machine(state="charging"), ["adopt", "set_amps"]
        if t.surplus_w < start_watts(tun):
            return Machine(state="idle", hold_s=0), []
        # Threshold is checked against the timer *as carried in*, not the
        # value after this tick's period is folded in. A period longer than
        # start_hold_s must still take two ticks to fire, or "sustained"
        # would mean nothing when the tick is coarser than the hold.
        if m.hold_s >= pol.start_hold_s:
            return Machine(state="charging"), ["charge_start", "set_amps"]
        return Machine(state="idle", hold_s=m.hold_s + t.period_s), []

    if m.state == "charging":
        if t.decision.floor_breach:
            breach = m.breach_ticks + 1
            if breach >= 2:      # dwell: one tick can be clock skew, not weather
                return Machine(state="grace"), ["set_amps"]
            return Machine(state="charging", breach_ticks=breach), []
        actions = ["set_amps"] if t.decision.write else []
        return Machine(state="charging"), actions

    if m.state == "grace":
        # Recovery needs a band above the re-entry point, or a surplus sitting
        # exactly at the floor chatters grace<->charging every tick.
        if t.decision.raw_target >= tun.min_a + 1:
            recover = m.recover_ticks + 1
            if recover >= 2:
                return Machine(state="charging"), ["set_amps"]
            # grace_wh must be carried, exactly as grace_s_elapsed is. A
            # surplus oscillating either side of the floor -- which is what a
            # cycling compressor produces -- would otherwise refund the
            # energy budget on every flicker, and the car could sit at the
            # floor importing forever without grace ever expiring.
            return Machine(state="grace", grace_s_elapsed=m.grace_s_elapsed,
                           grace_wh=m.grace_wh, recover_ticks=recover), []
        elapsed = m.grace_s_elapsed + t.period_s
        # RIDE-THROUGH. Grace is bounded by energy as well as time, and the
        # energy budget is what does the work.
        #
        # Measured 2026-07-27: an AC compressor pushed the site into import,
        # a 180 s grace expired, charge_stop fired at 12:58 -- and two
        # minutes later the compressor stopped and 4.7 kW began exporting
        # into a car held at 0 A by restart_hold_s until 13:06. A ~6 minute
        # compressor cycle simply outlasted a 3 minute grace.
        #
        # Holding at the floor through that cycle costs the car's own draw,
        # ~1.2 kW, for its duration. That is the quantity budgeted here --
        # not total site import, which includes the house and would charge
        # the car for the compressor's own consumption.
        car_w = tun.min_a * tun.volts        # grace pins the car at the floor
        grid_w = -(t.decision.error_w + tun.margin_w)   # exact: see control()
        spent = m.grace_wh + min(car_w, max(0.0, grid_w)) * t.period_s / 3600.0
        if spent >= pol.grace_budget_wh or elapsed > pol.grace_s:
            return Machine(state="stopped"), ["charge_stop", "restore"]
        return Machine(state="grace", grace_s_elapsed=elapsed, grace_wh=spent), []

    # stopped
    if t.surplus_w < start_watts(tun):
        return Machine(state="stopped", hold_s=0), []
    if m.hold_s >= pol.restart_hold_s:
        return Machine(state="charging"), ["wake", "charge_start", "set_amps"]
    return Machine(state="stopped", hold_s=m.hold_s + t.period_s), []


# --- charge_mode: the "now" override ---------------------------------------
#
# advance() above cannot express "charge regardless of the sun". With
# enabled=1 an externally started charge lands in idle with car_charging=True,
# is ADOPTED, has set_amps written against a negative night surplus, breaches
# the floor within two ticks, transits grace and stops -- about four billed
# commands to end exactly where it began. So force mode does not run advance()
# at all; it runs force_plan() instead, and the solar machine stays idle
# throughout.
#
# Mode is DERIVED, never stored: `enabled` keeps its exact meaning, so every
# existing test, the HA switch and the setup page stay correct.


def forcing(conf: dict, now: float) -> bool:
    """Whether a force is live right now.

    Compared against the clock, never merely tested for presence: the column
    is cleared by the collector on its next tick, so between expiry and that
    tick the timestamp is still there and still in the past.
    """
    until = conf.get("force_charge_until")
    return bool(until) and now < until


# --- the manual-override pause --------------------------------------------
#
# The owner and the controller both write charge_current_request, and until
# now the controller always won: move the slider in the Tesla app or on the
# car's own screen mid-engagement and the next tick wrote the solar-derived
# rate straight back over it. These two functions are the whole detection
# rule, and they are pure for the same reason advance() and force_plan() are
# -- the one real hazard here is mistaking ordinary command-propagation lag
# for an owner's decision, and that is a question about three values.


def override_step(commanded_amps: int | None, commanded_ack: bool,
                  charge_amps: int | None) -> tuple[bool, int | None]:
    """One step of the acknowledge-then-diverge handshake. Returns
    (ack_next, override_amps or None).

    NOT "the car reports something other than what we wrote". That is
    routinely true for a tick or two after every single write -- a command
    takes time to reach vehicle_data, and between writes the view is only
    refreshed every view_refresh_ticks (10 minutes at the defaults). Latching
    on it would disable automation for the rest of the session over nothing,
    which is far worse than the fight it was meant to end.

    So an override is a car that had already ADOPTED our value and then moved
    away from it::

        write 12 A        commanded=12 ack=0
        car reports 48    commanded=12 ack=0  -- not propagated yet, quiet
        car reports 12    commanded=12 ack=1  -- the car has adopted it
        car reports 32    commanded=12 ack=1  -- OVERRIDE, at 32 A

    Nothing but an outside writer can produce that sequence. The rule needs
    no clock, no dwell and no view timestamp: immunity to propagation lag is
    a property of its shape rather than of a tolerance someone has to tune.

    A controller rewrite resets the handshake at the call site (commanded_amps
    changes, ack goes back to False), so the tick where the car still reports
    the previous value cannot latch. A write the car never acknowledges never
    arms the latch at all, which is correct: control was never established, so
    there is nothing to take away.

    charge_amps is None when the view carries no charge_current_request.
    Absence is not evidence -- returning "no override" keeps a partial payload
    from switching the feature off.
    """
    if commanded_amps is None or charge_amps is None:
        return commanded_ack, None
    if charge_amps == commanded_amps:
        return True, None
    if commanded_ack:
        return True, charge_amps
    return False, None


def override_cleared(armed: bool, charging_state: str | None,
                     ) -> tuple[bool, bool]:
    """Whether a stop-and-start has released the pause. Returns
    (armed_next, cleared).

    Two observations, not one: charging must be seen to STOP (which arms
    this) and then to START again (which clears the pause). A single
    "charging" reading proves nothing -- the car was already charging when
    the owner took it over, and treating that as a restart would clear the
    pause on its very first tick.

    Everything outside LIVE_CHARGING_STATES arms, so Complete, Stopped,
    NoPower and Disconnected all count -- unplugging and plugging back in is
    a stop and a start like any other, and the owner meant it the same way.

    An unknown charging_state arms rather than clearing. Absence is not
    evidence that the car is charging, and arming is the safe direction: it
    can only delay a resume, never hand a car back to the controller while
    the owner is still driving it manually.
    """
    if charging_state in LIVE_CHARGING_STATES:
        return (False, True) if armed else (False, False)
    return True, False


def charge_mode(conf: dict, st: dict | None, now: float) -> str:
    """"now" | "off" | "manual" | "solar".

    Order is the argument. A live force outranks everything, because forcing
    is the owner taking control back explicitly and PUT /charge-mode clears
    the pause on its way past. "off" outranks "manual" next: the feature is
    switched off, and that the owner also once moved a slider is not the
    thing worth reporting.

    `st` may be None for a caller that has only the config -- a fresh install
    with no vehicle row yet. Mode stays derived, never stored: `enabled` keeps
    its exact meaning, so the HA switch and the setup page stay correct.
    """
    if forcing(conf, now):
        return "now"
    if not conf["enabled"]:
        return "off"
    if (st or {}).get("override_amps") is not None:
        return "manual"
    return "solar"


def next_midnight_ts(tz: str, now: float) -> int:
    """The next local midnight strictly after `now`.

    Adding a day to the aware datetime BEFORE replacing the time-of-day is
    what makes this correct across a DST boundary: replace-then-add would
    build a wall-clock midnight that does not exist on a spring-forward day.
    """
    zone = ZoneInfo(tz)
    tomorrow = datetime.fromtimestamp(now, zone) + timedelta(days=1)
    return int(tomorrow.replace(hour=0, minute=0, second=0,
                                microsecond=0).timestamp())


def force_plan(*, state: str, location: str, plugged: bool,
               car_charging: bool, amps_actual: int, amps_max: int,
               force_started: bool) -> tuple[list[str], bool]:
    """What to do this tick while charge_mode is "now". Pure.

    Returns (actions, force_started_next). Actions are names, not calls --
    collector.py performs them, exactly as with advance().

    Order matters. The unknown-location freeze comes first for the same
    reason it does in advance(): Tesla OMITS location keys rather than
    nulling them, so "scope revoked", "sharing off" and "genuinely elsewhere"
    are indistinguishable, and none of them is grounds to command a car.

    The hand-off comes second. The solar controller may be mid-engagement,
    holding the car at its 5 A floor with `dirty` set and `original_amps`
    recorded. Forcing on top of that would overwrite the amps the restore
    path exists to put back.
    """
    if location == "unknown":
        return [], force_started
    if state != "idle":
        return ["restore"], force_started
    if not plugged or location != "home":
        return [], force_started
    if car_charging:
        # Only write when the car is not already where we want it. The
        # controller's own "already holds this value" suppression, applied
        # here: steady state is zero commands per tick, not one every 120 s.
        actions = [] if amps_actual >= amps_max else ["force_amps"]
        return actions, True
    if force_started:
        # It ran and has stopped -- reaching the limit reports Complete.
        # Do NOT restart it: that would fight the owner's own stop forever.
        # force_expired() turns this into the expiry.
        return [], True
    return ["force_start", "force_amps"], False


def force_expired(*, force_charge_until: int | None, now: float,
                  car_charging: bool, force_started: bool) -> bool:
    """Whether the force should end now. Pure."""
    if not force_charge_until:
        return False
    if now >= force_charge_until:
        return True
    return bool(force_started) and not car_charging


SCHEMA = """
CREATE TABLE IF NOT EXISTS solar_config (
  id                INTEGER PRIMARY KEY CHECK (id = 1),
  enabled           INTEGER NOT NULL DEFAULT 0,
  period_s          INTEGER NOT NULL DEFAULT 120,
  margin_w          INTEGER NOT NULL DEFAULT 100,
  deadband_w        INTEGER NOT NULL DEFAULT 250,
  ramp_a            INTEGER NOT NULL DEFAULT 8,
  min_a             INTEGER NOT NULL DEFAULT 5,
  -- grace_s is an absolute cap; grace_budget_wh is the binding
  -- constraint in practice. See Policy.
  grace_s           INTEGER NOT NULL DEFAULT 900,
  grace_budget_wh   REAL NOT NULL DEFAULT 250.0,
  -- Meter-only watch cadence for a sleeping, plugged-in, hungry car.
  -- Costs ONE site request per tick and never touches the vehicle,
  -- so it can run far tighter than the vehicle poll it replaces.
  watch_s           INTEGER NOT NULL DEFAULT 300,
  -- Tariff. NULL means "not configured" and suppresses every money
  -- figure rather than showing a confident zero. These live here rather
  -- than in .env so they are editable without a restart or a file edit.
  import_rate       REAL,
  export_rate       REAL,
  restart_hold_s    INTEGER NOT NULL DEFAULT 300,
  start_hold_s      INTEGER NOT NULL DEFAULT 60,
  raise_hold_s      INTEGER NOT NULL DEFAULT 600,
  soc_ceiling       INTEGER NOT NULL DEFAULT 90,
  raise_limit       INTEGER NOT NULL DEFAULT 1,
  -- 400/day is a RUNAWAY BACKSTOP, set above the ~250 expected on a charging
  -- day. It is not a budget enforcer. The earlier default of 1200 would have
  -- been $72/month against a $10 credit.
  daily_request_cap INTEGER NOT NULL DEFAULT 400,
  view_refresh_ticks INTEGER NOT NULL DEFAULT 5,
  deadline_soc      INTEGER,
  deadline_hour     INTEGER,
  -- charge_mode = "now": a unix timestamp the force expires at, NULL when
  -- not forcing. A TIMESTAMP rather than a flag plus a day-stamp, so an app
  -- that is down at midnight expires late on its next tick instead of
  -- missing the rollover entirely.
  force_charge_until  INTEGER,
  -- Task 17b: the ratgdo garage opener. garage_close_hour is NULL = off, the
  -- same "absence means disabled" convention as deadline_hour above.
  garage_url          TEXT,
  garage_auto_open    INTEGER NOT NULL DEFAULT 0,
  garage_ring_m       INTEGER NOT NULL DEFAULT 800,
  garage_close_hour   INTEGER,
  garage_close_warn_s INTEGER NOT NULL DEFAULT 8,
  -- The owner and the controller both write charge_current_request. With
  -- this set, the owner wins: a rate they set from the Tesla app or the
  -- car's own screen pauses solar control until charging is stopped and
  -- started again, or it is re-enabled from the car page. Default 1 --
  -- whoever touched the car most recently and most deliberately should be
  -- the one driving it. Set to 0 for the older behaviour, where the
  -- controller writes its own rate straight back over theirs.
  pause_on_override INTEGER NOT NULL DEFAULT 1,
  updated_at        INTEGER NOT NULL DEFAULT 0
);

-- The machine's counters live here, not just its state name. Each tick is a
-- separate pass that reloads from the database, so a dwell counter held only
-- in memory would reset every time and the two-tick hysteresis would never
-- fire -- the exact flapping it exists to prevent.
CREATE TABLE IF NOT EXISTS solar_state (
  vin             TEXT PRIMARY KEY,
  state           TEXT    NOT NULL DEFAULT 'idle',
  breach_ticks    INTEGER NOT NULL DEFAULT 0,
  recover_ticks   INTEGER NOT NULL DEFAULT 0,
  grace_s_elapsed INTEGER NOT NULL DEFAULT 0,
  hold_s          INTEGER NOT NULL DEFAULT 0,
  -- Grid energy spent riding out a dip at the floor. Bounds grace alongside
  -- grace_s, and is the constraint that actually binds. See advance().
  grace_wh        REAL    NOT NULL DEFAULT 0,
  -- Liveness. Written every loop iteration regardless of what the tick
  -- decided; see STATE_NEW_COLUMNS for why tick age cannot answer this.
  heartbeat_ts      INTEGER,
  heartbeat_sleep_s INTEGER,
  -- The unbroken-watch anchor: when we last saw, without a gap, that this
  -- car was still asleep. See solar.asleep_confirmed.
  asleep_confirmed_ts INTEGER,
  dirty           INTEGER NOT NULL DEFAULT 0,
  original_amps   INTEGER,
  original_limit  INTEGER,
  raised_to       INTEGER,
  raise_hold_elapsed INTEGER NOT NULL DEFAULT 0,
  requests_today  INTEGER NOT NULL DEFAULT 0,
  requests_day    TEXT,
  capped          INTEGER NOT NULL DEFAULT 0,
  engaged_at      INTEGER,
  -- Invariant 4 (spec 3.7): consecutive 429s from live_status, and the
  -- backoff currently in force because of them. Persisted, not just held in
  -- memory, so a restart mid-rate-limit-event does not resume hammering at
  -- the normal cadence -- see migrate_state() below, which is what actually
  -- gets these two columns onto the live table.
  consecutive_429s INTEGER NOT NULL DEFAULT 0,
  backoff_s        INTEGER NOT NULL DEFAULT 0,
  -- Task 17b: the garage arrival latch (armed on leaving the ring, fired --
  -- and disarmed -- at most once per re-entry) and the once-per-day stamp
  -- for the scheduled close, same convention as requests_day above.
  garage_armed          INTEGER NOT NULL DEFAULT 0,
  garage_last_close_day TEXT,
  -- Have we yet OBSERVED the forced charge running? Right after charge_start
  -- the car reports Starting, or briefly still Stopped, so "not charging" is
  -- not evidence of an ended charge until this is set. Without it the mode
  -- expires on its own first tick.
  force_started         INTEGER NOT NULL DEFAULT 0,
  -- Task 18: the banked-solar ledger. solar_soc is percentage points of the
  -- CURRENT soc that came from the sun (see green.ledger_step) -- a stock,
  -- not a flow, tracked in SoC space so it needs no pack size and no
  -- consumption figure. ledger_soc is the SoC as of the last observation,
  -- needed to diff consecutive samples; NULL until the ledger has observed
  -- its first tick, which is also how it starts at 0 rather than a guess.
  -- ledger_stale marks a lower bound: a gap long enough, AND crossed by a
  -- SoC change, that the pack may have moved unobserved (see collector.py's
  -- call site for the threshold -- deliberately NOT store.GAP_SECONDS,
  -- which is tuned for the SoC chart's dashed-hole question, not this one).
  solar_soc       REAL    NOT NULL DEFAULT 0,
  ledger_soc      INTEGER,
  ledger_stale    INTEGER NOT NULL DEFAULT 0,
  -- When ledger_soc last CHANGED. A percentage point of an 81.6 kWh pack is
  -- ~13 minutes of charging against a 120 s tick, so the sun/grid split has
  -- to be integrated across that whole span rather than read off whichever
  -- tick crossed the integer -- see green.interval_solar_fraction.
  ledger_since_ts INTEGER,
  -- Task 20: lifetime free miles driven (see green.free_miles_step). A
  -- running total, never reset -- ledger_odo is the odometer as of the last
  -- observation (NULL until this ledger has watched its own first tick,
  -- same "absence means never assume a past" convention as ledger_soc
  -- above). free_miles_since is the timestamp collector.py stamped the
  -- first time ledger_odo was recorded, purely for display ("since 27
  -- Jul") -- never recomputed, never backfilled.
  free_miles_driven REAL    NOT NULL DEFAULT 0,
  tracked_miles     REAL    NOT NULL DEFAULT 0,
  ledger_odo        REAL,
  free_miles_since  INTEGER,
  -- The manual-override pause (see override_step/override_cleared).
  -- commanded_amps is the last rate the controller successfully wrote and
  -- commanded_ack whether the car has been OBSERVED reporting it back; an
  -- override is a divergence after acknowledgement, never a bare mismatch,
  -- which is routine for a tick or two after every write.
  --
  -- override_amps IS the paused state -- NOT NULL means paused -- rather
  -- than a boolean sitting beside the number. A separate flag could
  -- disagree with the value it describes; this cannot. It carries the
  -- owner's own rate, which is what the car page shows them.
  --
  -- override_armed is the first half of "stopped and started again":
  -- charging seen to STOP arms it, charging seen to start again clears the
  -- pause. One observation cannot do it -- the car was already charging
  -- when the owner took it over.
  commanded_amps  INTEGER,
  commanded_ack   INTEGER NOT NULL DEFAULT 0,
  override_amps   INTEGER,
  override_since  INTEGER,
  override_armed  INTEGER NOT NULL DEFAULT 0,
  updated_at      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS solar_ticks (
  ts           INTEGER NOT NULL,
  vin          TEXT    NOT NULL,
  state        TEXT    NOT NULL,
  grid_w       REAL, solar_w REAL, car_w REAL, surplus_w REAL, error_w REAL,
  amps_before  INTEGER, amps_target INTEGER, amps_written INTEGER,
  soc          INTEGER,
  import_w     REAL,
  period_s     INTEGER,
  note         TEXT,
  PRIMARY KEY (vin, ts)
);
CREATE INDEX IF NOT EXISTS solar_ticks_state ON solar_ticks (vin, state, ts);
"""

CONFIG_DEFAULTS = {
    "enabled": 0, "period_s": 120, "margin_w": 100, "deadband_w": 250,
    "ramp_a": 8, "min_a": 5, "grace_s": 900,
    "grace_budget_wh": 250.0,
    "import_rate": None, "export_rate": None, "watch_s": 300, "restart_hold_s": 300,
    "start_hold_s": 60, "raise_hold_s": 600, "soc_ceiling": 90,
    "raise_limit": 1, "daily_request_cap": 400, "view_refresh_ticks": 5,
    "deadline_soc": None, "deadline_hour": None,
    "garage_url": None, "garage_auto_open": 0, "garage_ring_m": 800,
    "garage_close_hour": None, "garage_close_warn_s": 8,
    "force_charge_until": None,
    "pause_on_override": 1,
}

STATE_DEFAULTS = {
    "state": "idle", "breach_ticks": 0, "recover_ticks": 0,
    "grace_s_elapsed": 0, "hold_s": 0, "grace_wh": 0.0,
    "dirty": 0, "original_amps": None, "original_limit": None,
    "raised_to": None, "raise_hold_elapsed": 0,
    "requests_today": 0, "requests_day": None,
    "capped": 0, "engaged_at": None,
    "consecutive_429s": 0, "backoff_s": 0,
    "garage_armed": 0, "garage_last_close_day": None,
    "force_started": 0,
    "solar_soc": 0.0, "ledger_soc": None, "ledger_stale": 0,
    "ledger_since_ts": None,
    "free_miles_driven": 0.0, "tracked_miles": 0.0, "ledger_odo": None,
    "free_miles_since": None,
    "heartbeat_ts": None, "heartbeat_sleep_s": None,
    "asleep_confirmed_ts": None,
    "commanded_amps": None, "commanded_ack": 0,
    "override_amps": None, "override_since": None, "override_armed": 0,
}

# The Machine fields that must survive between ticks. Anything here that is
# not persisted silently disables the dwell and hysteresis logic.
MACHINE_FIELDS = ("state", "breach_ticks", "recover_ticks",
                  "grace_s_elapsed", "hold_s", "grace_wh")


def machine_from(st: dict) -> Machine:
    return Machine(**{k: st[k] for k in MACHINE_FIELDS})


def machine_fields(m: Machine) -> dict:
    return {k: getattr(m, k) for k in MACHINE_FIELDS}


# Columns added to solar_state AFTER it first shipped. `CREATE TABLE IF NOT
# EXISTS` in SCHEMA is a no-op against a table that already exists -- see
# store.py's _migrate, which this mirrors -- so a schema edit alone never
# reaches a live car.db whose solar_state predates the column. migrate_state()
# must run once at startup, after SCHEMA is applied, before any load_state or
# save_state call.
STATE_NEW_COLUMNS = (
    ("raise_hold_elapsed", "INTEGER NOT NULL DEFAULT 0"),
    ("consecutive_429s", "INTEGER NOT NULL DEFAULT 0"),
    ("backoff_s", "INTEGER NOT NULL DEFAULT 0"),
    ("garage_armed", "INTEGER NOT NULL DEFAULT 0"),
    ("garage_last_close_day", "TEXT"),
    # Task 18: the banked-solar ledger -- see the SCHEMA comment above.
    ("solar_soc", "REAL NOT NULL DEFAULT 0"),
    ("ledger_soc", "INTEGER"),
    ("ledger_stale", "INTEGER NOT NULL DEFAULT 0"),
    # When ledger_soc last CHANGED, so the attribution can be integrated
    # across the whole percentage point rather than sampled at whichever
    # tick crossed it -- see green.interval_solar_fraction. A bookmark like
    # ledger_soc and ledger_odo, not a counter: nothing accumulates in it,
    # so it cannot drift away from the tick log the way a stored total can.
    ("ledger_since_ts", "INTEGER"),
    # Task 20: lifetime free miles driven -- see the SCHEMA comment above.
    ("free_miles_driven", "REAL NOT NULL DEFAULT 0"),
    ("tracked_miles", "REAL NOT NULL DEFAULT 0"),
    ("ledger_odo", "REAL"),
    ("free_miles_since", "INTEGER"),
    # Ride-through: grid energy spent holding at the floor through a dip.
    ("grace_wh", "REAL NOT NULL DEFAULT 0"),
    # Liveness, for anything asking "is the collector actually running?".
    # solar_ticks cannot answer that: a tick logs a row only when the
    # controller runs, and the loop legitimately writes none when solar is
    # disabled, after dark, or while the car sleeps. Judging liveness by tick
    # age would report the collector dead every night.
    ("heartbeat_ts", "INTEGER"),
    ("heartbeat_sleep_s", "INTEGER"),
    # The unbroken-watch anchor. NULL on every existing install, which is the
    # correct starting point: a chain nobody has built yet is a chain we
    # cannot claim, so the snapshot cap governs until the car next wakes.
    ("asleep_confirmed_ts", "INTEGER"),
    # charge_mode "now": the latch that stops the force expiring on its own
    # first tick -- see the SCHEMA comment above.
    ("force_started", "INTEGER NOT NULL DEFAULT 0"),
    # The manual-override pause -- see the SCHEMA comment above. This is the
    # only path onto the owner's live solar_state, whose CREATE TABLE was a
    # no-op the moment the table first existed.
    ("commanded_amps", "INTEGER"),
    ("commanded_ack", "INTEGER NOT NULL DEFAULT 0"),
    ("override_amps", "INTEGER"),
    ("override_since", "INTEGER"),
    ("override_armed", "INTEGER NOT NULL DEFAULT 0"),
)


def migrate_state(db: sqlite3.Connection) -> None:
    """Add columns to an EXISTING solar_state table."""
    existing = {row[1] for row in db.execute("PRAGMA table_info(solar_state)")}
    for name, decl in STATE_NEW_COLUMNS:
        if name not in existing:
            db.execute(f"ALTER TABLE solar_state ADD COLUMN {name} {decl}")


# Columns added to solar_config AFTER it first shipped -- the first ones ever
# needed here. Same hazard as STATE_NEW_COLUMNS above: CREATE TABLE IF NOT
# EXISTS in SCHEMA is a no-op against the owner's live solar_config, which
# already has rows in it, so a schema edit alone never reaches it.
# migrate_config() must run once at startup, after SCHEMA is applied, before
# any load_config or save_config call -- mirrored in store.py right next to
# migrate_state().
CONFIG_NEW_COLUMNS = (
    ("garage_url", "TEXT"),
    ("garage_auto_open", "INTEGER NOT NULL DEFAULT 0"),
    ("garage_ring_m", "INTEGER NOT NULL DEFAULT 800"),
    ("garage_close_hour", "INTEGER"),
    ("garage_close_warn_s", "INTEGER NOT NULL DEFAULT 8"),
    # Ride-through: the energy budget that bounds grace. CREATE TABLE IF
    # NOT EXISTS is a no-op on the owner's existing table, so this is
    # the only path by which a live database gains the column.
    ("grace_budget_wh", "REAL NOT NULL DEFAULT 250.0"),
    ("import_rate", "REAL"),
    ("export_rate", "REAL"),
    ("watch_s", "INTEGER NOT NULL DEFAULT 300"),
    # charge_mode "now": when the force expires -- see the SCHEMA comment.
    ("force_charge_until", "INTEGER"),
    # The manual-override pause. Defaults to 1, so an existing install gains
    # the protection on upgrade without touching the setup page -- the
    # behaviour it replaces is the one nobody asked for.
    ("pause_on_override", "INTEGER NOT NULL DEFAULT 1"),
)


def migrate_config(db: sqlite3.Connection) -> None:
    """Add columns to an EXISTING solar_config table."""
    existing = {row[1] for row in db.execute("PRAGMA table_info(solar_config)")}
    for name, decl in CONFIG_NEW_COLUMNS:
        if name not in existing:
            db.execute(f"ALTER TABLE solar_config ADD COLUMN {name} {decl}")


def _begin_immediate(db: sqlite3.Connection) -> bool:
    """Take SQLite's write lock BEFORE the read half of a read-modify-write.

    Without it, two processes can both SELECT the old row, both compute an
    update from it, and the second write silently discards the first. Returns
    whether this call opened the transaction, so nested use does not commit
    someone else's work out from under them.
    """
    if db.in_transaction:
        return False
    db.execute("BEGIN IMMEDIATE")
    return True


def load_config(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT * FROM solar_config WHERE id = 1").fetchone()
    if row is None:
        return dict(CONFIG_DEFAULTS)
    return {k: row[k] for k in CONFIG_DEFAULTS}


# WRITER SEPARATION, relied upon by the whole design: solar_config is written
# only by the web app (the owner editing settings) and solar_state only by the
# collector (one row per vehicle, one process). They never write the same table,
# so cross-process contention on a single row does not arise in the current
# wiring. BEGIN IMMEDIATE above is defence in depth, and this comment is the
# thing to re-read before adding a config write to the collector or a state
# write to the web app.
#
# ONE deliberate exception, added with charge_mode: the collector clears
# force_charge_until when a force expires. Nothing else can observe local
# midnight, and the web app may not be running. It is confined to that single
# field -- collector.py's force branch carries the same note.
def save_config(db: sqlite3.Connection, **fields) -> None:
    unknown = set(fields) - set(CONFIG_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown solar_config fields: {sorted(unknown)}")
    owned = _begin_immediate(db)
    try:
        current = load_config(db)
        current.update(fields)
        columns = list(CONFIG_DEFAULTS)
        db.execute(
            f"""INSERT INTO solar_config (id, {', '.join(columns)}, updated_at)
                VALUES (1, {', '.join('?' * len(columns))}, ?)
                ON CONFLICT(id) DO UPDATE SET
                  {', '.join(f'{c} = excluded.{c}' for c in columns)},
                  updated_at = excluded.updated_at""",
            [current[c] for c in columns] + [int(time.time())],
        )
    except BaseException:
        # Roll back ONLY what we opened. Leaving it open would make
        # _begin_immediate see in_transaction on every later call, return
        # owned=False, and silently skip the commit forever -- turning one
        # transient error into a permanent persistence outage on a connection
        # that lives as long as the process.
        if owned:
            db.rollback()
        raise
    else:
        if owned:
            db.commit()


def tunables_from(cfg: dict, amps_max: int | None, volts: int | None) -> Tunables:
    """Config plus whatever the car actually reports.

    amps_max comes from charge_current_request_max (the session ceiling, which
    changes when the car is replugged elsewhere) and volts from charger_voltage,
    which is only meaningful mid-session. Both fall back to this site's measured
    nominal.
    """
    return Tunables(
        margin_w=cfg["margin_w"], deadband_w=cfg["deadband_w"],
        ramp_a=cfg["ramp_a"], min_a=cfg["min_a"],
        # Truthy, not `is None`, deliberately: a zero from either field would
        # reach control()'s `error_w / volts` and raise ZeroDivisionError.
        # Coercing a nonsensical zero to the documented nominal is the safe
        # failure.
        max_a=amps_max if amps_max else 48,
        volts=volts if volts else 240,
    )


def policy_from(cfg: dict) -> Policy:
    return Policy(grace_s=cfg["grace_s"],
                  grace_budget_wh=cfg["grace_budget_wh"],
                  restart_hold_s=cfg["restart_hold_s"],
                  start_hold_s=cfg["start_hold_s"], enabled=bool(cfg["enabled"]))


def load_state(db: sqlite3.Connection, vin: str) -> dict:
    row = db.execute("SELECT * FROM solar_state WHERE vin = ?", (vin,)).fetchone()
    if row is None:
        return dict(STATE_DEFAULTS)
    return {k: row[k] for k in STATE_DEFAULTS}


# WRITER SEPARATION, relied upon by the whole design: solar_config is written
# only by the web app (the owner editing settings) and solar_state only by the
# collector (one row per vehicle, one process). They never write the same table,
# so cross-process contention on a single row does not arise in the current
# wiring. BEGIN IMMEDIATE above is defence in depth, and this comment is the
# thing to re-read before adding a config write to the collector or a state
# write to the web app.
def save_state(db: sqlite3.Connection, vin: str, **fields) -> None:
    unknown = set(fields) - set(STATE_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown solar_state fields: {sorted(unknown)}")
    owned = _begin_immediate(db)
    try:
        current = load_state(db, vin)
        current.update(fields)
        columns = list(STATE_DEFAULTS)
        db.execute(
            f"""INSERT INTO solar_state (vin, {', '.join(columns)}, updated_at)
                VALUES (?, {', '.join('?' * len(columns))}, ?)
                ON CONFLICT(vin) DO UPDATE SET
                  {', '.join(f'{c} = excluded.{c}' for c in columns)},
                  updated_at = excluded.updated_at""",
            [vin] + [current[c] for c in columns] + [int(time.time())],
        )
    except BaseException:
        # Roll back ONLY what we opened. Leaving it open would make
        # _begin_immediate see in_transaction on every later call, return
        # owned=False, and silently skip the commit forever -- turning one
        # transient error into a permanent persistence outage on a connection
        # that lives as long as the process.
        if owned:
            db.rollback()
        raise
    else:
        if owned:
            db.commit()


def log_tick(db: sqlite3.Connection, vin: str, **fields) -> None:
    """Every tick is logged, written or not, so a quiet loop and a broken loop
    look different in the record."""
    columns = ("ts", "state", "grid_w", "solar_w", "car_w", "surplus_w", "error_w",
               "amps_before", "amps_target", "amps_written", "soc", "import_w",
               "period_s", "note")
    unknown = set(fields) - set(columns)
    if unknown:
        raise ValueError(f"unknown solar_ticks fields: {sorted(unknown)}")
    db.execute(
        f"""INSERT OR REPLACE INTO solar_ticks (vin, {', '.join(columns)})
            VALUES (?, {', '.join('?' * len(columns))})""",
        [vin] + [fields.get(c) for c in columns],
    )
    db.commit()


def count_request(db: sqlite3.Connection, vin: str, today: str) -> tuple[int, bool]:
    """Increment the daily request counter. Returns (count, capped).

    Denominated in REQUESTS, not dollars: Tesla no longer publishes
    per-request data pricing, so a dollar cap would be a guess dressed as a
    limit.

    NOT atomic end to end -- the read happens outside the transaction that
    save_state opens, so two concurrent callers could both read the same count
    and one increment would be lost. Safe only under the writer-separation
    invariant documented above save_state: solar_state is written by the
    collector alone, and there is exactly one collector process. Re-read that
    invariant before calling this from anywhere else.
    """
    st = load_state(db, vin)
    count = st["requests_today"] + 1 if st["requests_day"] == today else 1
    cap = load_config(db)["daily_request_cap"]
    capped = count >= cap
    save_state(db, vin, requests_today=count, requests_day=today,
               capped=1 if capped else 0)
    return count, capped


def may_restore(dirty: int, location: str, online: bool, proxy_up: bool) -> bool:
    """Whether a crash-recovery restore may be attempted right now.

    Restoring writes amps and a charge limit to the car. Doing that
    unconditionally at startup would push home settings into whatever session
    the car is actually in -- including a Supercharger. If any gate fails the
    dirty flag STAYS SET and we retry next tick; it is never cleared by
    giving up.
    """
    return bool(dirty) and location == "home" and online and proxy_up


# A site that is producing this little is producing nothing worth waking a
# car for: the car's own floor is min_a * volts, ~1,200 W. Set well below
# that, so the back-off engages only in genuine darkness -- deep dusk, or an
# array under snow -- and never during a merely cloudy afternoon that the
# controller should still be watching.
DARK_W = 150.0

# Consecutive dark readings before standing down, so one missing or zero
# sample at dusk cannot park the watch for the night.
DARK_TICKS = 3


# How old a reading may be and still say anything about NOW. Two hours is
# several stand-down intervals, so a healthy loop always has fresher data,
# while last night's zeros can never vouch for this morning.
DARK_MAX_AGE_S = 2 * 3600


def is_dark_at(recent: list[dict], now_ts: float) -> bool:
    """True when the array has recently been producing essentially nothing.

    Used to stand the meter watch down overnight. Derived from the site's own
    production rather than a clock or a sunrise table: no new dependency, no
    timezone arithmetic, and automatically right in December, during an
    eclipse, and under a foot of snow.

    FRESHNESS IS THE LOAD-BEARING PART, and its absence caused a real
    deadlock on 2026-07-29. solar_ticks only gains a row when a tick actually
    runs, so any tick that returns early without logging leaves yesterday's
    zeros as the newest rows. The back-off then read those zeros, held the
    loop at 1800 s, and slept straight through a sunny morning -- seeing dawn
    required a tick, and the back-off had suppressed the tick. A stale
    reading must therefore mean "we do not know", never "it is dark".

    Requires several consecutive dark readings, so one missing or zero sample
    at dusk cannot park the watch for the night. Fails OPEN throughout: with
    too little history, or stale history, keep watching -- an unnecessary
    tick costs $0.002 and sleeping through a sunny morning costs the feature.
    """
    if len(recent) < DARK_TICKS:
        return False
    for row in recent[:DARK_TICKS]:
        watts, ts = row.get("solar_w"), row.get("ts")
        if watts is None or ts is None:
            return False
        if now_ts - ts > DARK_MAX_AGE_S:
            return False
        if watts > DARK_W:
            return False
    return True


def is_dark(recent_solar_w: list[float]) -> bool:
    """Freshness-free variant, kept for the pure unit tests. Prefer
    is_dark_at, which is the one the collector uses -- a reading with no
    timestamp cannot be checked for staleness, and staleness is what made
    this deadlock."""
    if len(recent_solar_w) < DARK_TICKS:
        return False
    return all(w is not None and w <= DARK_W
               for w in recent_solar_w[:DARK_TICKS])


def recent_solar(db: sqlite3.Connection, vin: str, limit: int) -> list[dict]:
    """The last `limit` LOGGED (ts, solar_w) readings for vin, newest first.

    Feeds is_dark_at(). The TIMESTAMP is not decoration: solar_ticks only
    gains a row when a tick runs, so without it last night's zeros look
    exactly like this minute's and the watch can stand itself down through a
    sunny morning -- which it did, on 2026-07-29.
    """
    rows = db.execute(
        """SELECT ts, solar_w FROM solar_ticks
           WHERE vin = ? AND solar_w IS NOT NULL
           ORDER BY ts DESC LIMIT ?""",
        (vin, limit),
    ).fetchall()
    return [{"ts": row["ts"], "solar_w": row["solar_w"]} for row in rows]


def recent_grid_w(db: sqlite3.Connection, vin: str, limit: int) -> list[float]:
    """The last `limit` LOGGED grid_w readings for vin, newest first.

    Feeds grid_is_stuck(): the stuck-meter detector needs the raw tick
    history, not a derived quantity, and solar_ticks already logs grid_w
    every tick regardless of whether anything changed -- the history is
    already there for the reading.
    """
    rows = db.execute(
        """SELECT grid_w FROM solar_ticks
           WHERE vin = ? AND grid_w IS NOT NULL
           ORDER BY ts DESC LIMIT ?""",
        (vin, limit),
    ).fetchall()
    return [row["grid_w"] for row in rows]


def ticks_since(db: sqlite3.Connection, vin: str,
                since_ts: int) -> list[dict]:
    """Every logged tick for vin strictly after since_ts, oldest first.

    Feeds green.interval_solar_fraction, which needs the whole span a
    percentage point of SoC accumulated over rather than the single tick
    that happened to cross the integer. Selects only the four columns that
    attribution reads, the same set solar_routes._solar_ticks uses, so the
    two can never disagree about what a tick is.

    Strictly after, not on or after: since_ts is the moment the SoC last
    changed, and the tick that produced that change was already credited to
    the previous point. Including it would count it twice.
    """
    rows = db.execute(
        "SELECT state, car_w, grid_w, period_s FROM solar_ticks"
        " WHERE vin = ? AND ts > ? ORDER BY ts", (vin, since_ts),
    ).fetchall()
    return [dict(row) for row in rows]


def last_tick_ts(db: sqlite3.Connection, vin: str) -> int | None:
    """The timestamp of the most recently logged tick for vin, or None
    before the first one has ever been logged.

    Feeds the banked-solar ledger's gap detection (green.ledger_step):
    elapsed real time since the last observation, read back from disk
    rather than held in memory, so a process restart is measured correctly
    too -- a crash is exactly the kind of unobserved gap the ledger's
    staleness flag exists to catch. Must be read BEFORE this tick's own
    log_tick() call, or MAX(ts) would return this tick's own row and the
    gap would always read as zero.
    """
    row = db.execute(
        "SELECT MAX(ts) AS ts FROM solar_ticks WHERE vin = ?", (vin,)
    ).fetchone()
    return row["ts"] if row and row["ts"] is not None else None


def grace_import_wh(db: sqlite3.Connection, vin: str, since_ts: int) -> float:
    """Watt-hours imported while riding out a cloud.

    ONLY grace ticks. Summing every tick would total ordinary house import and
    the no-grid-electrons promise would become unmeasurable.
    """
    row = db.execute(
        """SELECT COALESCE(SUM(import_w * period_s), 0) / 3600.0 AS wh
           FROM solar_ticks
           WHERE vin = ? AND state = 'grace' AND ts >= ? AND import_w > 0""",
        (vin, since_ts),
    ).fetchone()
    return float(row["wh"])
