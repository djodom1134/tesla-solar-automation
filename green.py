"""Free miles: how far the car could go for free, on sun already banked in
the pack.

The owner's question -- "how far could we go for free, on sun stored in the
battery?" -- is three numbers multiplied together, and each one is hard for a
different reason. This module only multiplies and divides; `collector.py`
writes the tick log and `solar_routes.py` reads the sample history and hands
both in as plain rows. Pure functions only here -- no database, no network,
no clock -- so the arithmetic is testable without either.

The doctrine that matters more than any formula: every function below
returns `None` rather than a guess, and returns its own sample size alongside
it, so the caller can say exactly how sure it is, and why, instead of just
going quiet. A number that is absent for a stated reason is more useful than
one that is absent silently, and far more useful than one that is wrong.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Below this swing, integer-percent-quantised SoC dominates the estimate: a
# session's own reporting error is comparable to the swing itself, so the
# implied pack size is noise wearing the shape of a number.
MIN_SESSION_SOC = 10

# One qualifying session still carries a real error bar (see
# MIN_SESSION_SOC). A second, independent session's disagreement is itself
# informative, and combining totals (see pack_kwh) lets a big clean session
# outweigh a small noisy one rather than being averaged down to its level.
MIN_SESSIONS = 2

# Shorter than this, the measured efficiency is weather and terrain, not the
# car's actual consumption -- it swings far more over 5 miles than over 50.
MIN_MILES = 50

# States in which the controller is the only thing modulating the car's
# draw, so the attribution below is exact rather than pro-rata. "idle" may or
# may not show the car charging on its own schedule -- the controller isn't
# holding it to a solar-derived rate, so nothing here can be credited to the
# sun. "stopped" means the controller told the car to stop drawing current
# entirely; whatever happens next belongs to no one's decision the sun made.
ENGAGED_STATES = {"charging", "grace"}


def tick_solar_w(car_w: float, grid_w: float) -> float:
    """Solar attribution for ONE tick: whatever the car drew, minus whatever
    was being imported at that instant, came from the sun::

        solar_w = max(0, car_w - max(grid_w, 0))

    Deliberately conservative -- import is charged wholly against the car
    even though some of it fed the house, and importing more than the car
    drew means none of it was solar. A lower bound is the right kind of
    wrong for a number whose entire purpose is to be trustworthy.

    Shared by solar_kwh (which sums this over a tick log) and the
    banked-solar ledger (which only needs to know whether THIS tick was
    positive) -- one formula, so the two can never quietly drift apart.
    """
    return max(0.0, car_w - max(grid_w, 0.0))


def solar_kwh(ticks: list[dict[str, Any]]) -> float:
    """Solar energy delivered to the car, from the controller's own tick log.

    The controller is the only thing modulating the car's draw, so within an
    engaged tick the attribution is exact rather than pro-rata -- see
    tick_solar_w for the per-tick formula.
    """
    wh = 0.0
    for tick in ticks:
        if tick.get("state") not in ENGAGED_STATES:
            continue
        car_w = float(tick.get("car_w") or 0)
        grid_w = float(tick.get("grid_w") or 0)
        period_s = float(tick.get("period_s") or 0)
        wh += tick_solar_w(car_w, grid_w) * period_s / 3600
    return wh / 1000


def grid_kwh(ticks: list[dict[str, Any]]) -> float:
    """Utility energy delivered to the car, the other half of solar_kwh.

    DERIVED from the tick log rather than accumulated into a counter, and
    deliberately so. A stored counter starts at zero on the day it is added,
    so it disagrees with every figure computed from history until enough time
    passes to hide the gap -- which is exactly how "charged so far" came to
    contradict "banked solar" on the card. The tick log is never pruned and
    already answers this question for any window, so there is nothing for a
    counter to add except a way to drift.
    """
    wh = 0.0
    for tick in ticks:
        if tick.get("state") not in ENGAGED_STATES:
            continue
        car_w = float(tick.get("car_w") or 0)
        grid_w = float(tick.get("grid_w") or 0)
        period_s = float(tick.get("period_s") or 0)
        wh += tick_grid_w(car_w, grid_w) * period_s / 3600
    return wh / 1000


def charged_split(ticks: list[dict[str, Any]]) -> tuple[float, float]:
    """Lifetime energy into the car as (solar_kwh, grid_kwh), over EVERY tick
    the car actually drew -- not only the ticks the controller was driving.

    This is the difference between "how did the controller do" and "what is
    actually in this car", and only the second one is honest on a card that
    also shows a percentage. Scoping it to engaged ticks silently omits every
    charge the owner started themselves at full rate from the grid: on this
    site that read 61% solar against a true 29%, because solar_kwh's
    ENGAGED_STATES filter dropped the manual sessions from the denominator
    while leaving them in the pack.

    The banked ledger already counts those sessions -- grid charging dilutes
    the bank -- so anything shown beside it must count them too, or the two
    figures describe different cars.

    solar_kwh() keeps its narrower, controller-scoped meaning: it feeds
    free_miles, which genuinely asks what the controller captured.
    """
    solar_wh = grid_wh = 0.0
    for tick in ticks:
        car_w = float(tick.get("car_w") or 0)
        if car_w <= 0:
            continue
        grid_w = float(tick.get("grid_w") or 0)
        period_s = float(tick.get("period_s") or 0)
        solar_wh += tick_solar_w(car_w, grid_w) * period_s / 3600
        grid_wh += tick_grid_w(car_w, grid_w) * period_s / 3600
    return solar_wh / 1000, grid_wh / 1000


def pack_kwh(sessions: list[dict[str, Any]]) -> tuple[float | None, int]:
    """Usable pack size, measured from real charge sessions rather than
    assumed or taken from a spec sheet.

    Each qualifying session (see MIN_SESSION_SOC) implies
    ``pack_kwh = kwh_added / (soc_swing / 100)``. The combined estimate sums
    kwh_added and swing across every qualifying session and divides the
    totals, rather than averaging each session's own ratio -- the same
    principle energy.py uses for self_sufficiency: a big, clean session
    should outweigh a small, noisy one, not be averaged down to its level.

    Returns (None, n) below MIN_SESSIONS qualifying sessions: reporting a
    single session's implied pack size as fact would put fabricated
    precision underneath every number that depends on it.
    """
    added = 0.0
    swing = 0.0
    n = 0
    for session in sessions:
        pct = (session.get("soc_end") or 0) - (session.get("soc_start") or 0)
        if pct < MIN_SESSION_SOC:
            continue
        n += 1
        added += float(session.get("kwh_added") or 0)
        swing += pct / 100
    if n < MIN_SESSIONS or swing <= 0:
        return None, n
    return added / swing, n


def miles_per_kwh(
    segments: list[dict[str, Any]], pack_kwh: float | None
) -> tuple[float | None, float]:
    """This car's own measured efficiency, from odometer and SoC deltas
    across driving segments -- not the EPA rating, which no owner's car
    matches on the roads they actually drive.

    Needs the pack size to turn a SoC swing into kWh at all, and needs at
    least MIN_MILES of driving before the ratio means more than the weather
    and terrain of one particular trip (see MIN_MILES). Always returns the
    actual miles sampled alongside, even when that falls short, so the
    caller can say exactly how much more driving it needs.

    Each segment's SoC drop is floored at zero for the same reason
    solar_kwh's per-tick attribution is: a regen-heavy segment that gained
    charge should not be allowed to *subtract* from the energy the rest of
    the sample says was spent.
    """
    miles = sum(float(segment.get("miles") or 0) for segment in segments)
    if pack_kwh is None or miles < MIN_MILES:
        return None, miles
    pct = sum(max(0.0, float(segment.get("soc_drop") or 0)) for segment in segments)
    kwh = pack_kwh * pct / 100
    if kwh <= 0:
        return None, miles
    return miles / kwh, miles


def free_miles(solar_kwh: float | None, mi_per_kwh: float | None) -> float | None:
    """The owner's actual question, answered rather than approximated: how
    far the sun banked in the pack takes this car, at how this car actually
    drives -- not a guess standing in for either half."""
    if solar_kwh is None or mi_per_kwh is None:
        return None
    return solar_kwh * mi_per_kwh


# --------------------------------------------------------------------------
# Task 18: the banked-solar ledger -- the STOCK question ("how much sun is
# in the pack right now"), as opposed to solar_kwh's FLOW ("how much came in
# today"). Tracked in percentage points of SoC, not kWh, so the ledger needs
# no pack size and no consumption figure -- both unknown and both only
# entering at *display* time (see banked_miles_rated/banked_miles_measured
# below). See the module docstring's doctrine: never guess, say why.
# --------------------------------------------------------------------------

def tick_grid_w(car_w: float, grid_w: float) -> float:
    """The utility half of one tick's car draw: whatever tick_solar_w did not
    claim.

    A car pulling 2,000 W while the site imports 100 W is running 1,900 W of
    sunshine and 100 W of utility. Defined as the complement rather than
    independently, so solar + grid is exactly the draw by construction and
    the two can never disagree about the same tick.
    """
    return max(0.0, car_w) - tick_solar_w(car_w, grid_w)


def tick_solar_fraction(car_w: float, grid_w: float) -> float:
    """What proportion of ONE tick's car draw was sunshine, in [0, 1].

    Built on tick_solar_w for the same reason tick_grid_w is: the ledger, the
    daily solar_kwh total and the live ticker must all answer this question
    identically or the card contradicts itself.
    """
    if car_w <= 0:
        return 0.0
    return tick_solar_w(car_w, grid_w) / car_w


def ledger_step(solar_soc: float, soc_before: int | None, soc_now: int,
                solar_fraction: float, gap_s: int,
                gap_threshold_s: int) -> tuple[float, bool]:
    """Advance the banked-solar ledger by one observation.

    Returns (new_solar_soc, stale). Pure: no clock, no database, no network
    -- the caller supplies the previous SoC (persisted as solar_state.
    ledger_soc, since there is no reliable way to re-derive "the previous
    sample" inside a pure function), the elapsed seconds since the last
    observation (gap_s), and the gap length that counts as suspicious for
    ITS purposes (gap_threshold_s -- see below for why this must not be
    store.GAP_SECONDS), and gets back the new ledger value plus whether this
    observation is stale.

    ENTERING the pack: a rise in SoC banks the solar PROPORTION of that rise,
    `delta * solar_fraction`, where solar_fraction is this tick's measured
    split (see car_power_split). The remainder dilutes the bank exactly as a
    pure grid charge does -- total SoC went up, so the solar percentage falls
    on its own; grid electrons dilute the sun already banked, they do not
    remove it.

    This used to take a BOOLEAN, and banked the whole rise whenever the
    controller was engaged. That overstated the bank on every tick where the
    car drew more than the surplus: a car pulling 2,000 W against 1,900 W of
    surplus is 5% utility-powered, and recording it as 100% solar makes the
    ledger flattering rather than true. A fraction of 0.0 reproduces the old
    grid-charging branch exactly, and 1.0 the old solar branch.

    LEAVING the pack: any drop removes proportionally --
    ``solar_soc -= drop * (solar_soc / soc_before)``, equivalently
    ``solar_soc *= soc_now / soc_before``. You cannot drive on "the solar
    electrons" specifically: a pack that is 40% solar delivers 40% solar to
    whatever drew the drop -- the motor, vampire drain, Sentry,
    preconditioning, all alike, with no special case for any of them. A drop
    all the way to 0% SoC leaves exactly 0 banked, by this same formula.

    soc_before is None on the very first observation after this column
    existed at all -- a fresh install, or the tick right after this feature
    was deployed. There is nothing yet to diff against, and the only honest
    move is to record the observation and bank nothing: never assume a rise
    or a drop happened before anything was watching.

    STALENESS IS ABOUT UNOBSERVED CHANGE, NOT ELAPSED TIME. A long gap with
    an UNCHANGED SoC means the car sat asleep and nothing was missed at all
    -- flagging that would fire on perfectly ordinary idle polling, where
    the collector's own cadence can legitimately be as long as
    poll_asleep/poll_idle. What actually makes the bank untrustworthy is a
    gap that is BOTH long AND crossed by a change in SoC: the two-point diff
    this function does can't see whether that change happened all at once,
    or rose and fell several times in between (charged away from home, then
    driven) -- either way, the branch above that ran for it is a guess about
    something this function never actually watched happen. So: stale =
    gap_s > gap_threshold_s AND soc_now != soc_before. A long, quiet gap is
    not stale; a short, busy one is not stale either (ordinary accounting,
    seen and accounted for tick by tick) -- only the combination is.

    gap_threshold_s is a PARAMETER, not read from store.GAP_SECONDS, on
    purpose: that constant is tuned for the SoC chart's own question (when
    is a gap worth drawing as a dashed hole), an entirely different decision
    from "how long can this car plausibly sleep." Coupling the two meant
    that raising the poll-asleep interval for API budget reasons (to 1800s,
    equal to GAP_SECONDS) made the collector's own ordinary idle cadence
    trip this flag on nothing -- see collector.py's call site for the
    derived value actually used.

    The general invariant 0 <= solar_soc <= soc_now is enforced by a final
    clamp regardless of path, and a clamp that actually changes the value is
    logged -- by construction it should never fire from a consistent
    soc_before/solar_soc pair, so if it does, the caller's bookkeeping (or
    the car) did something this function was never told about, and a silent
    clamp would hide exactly that.
    """
    if soc_before is None:
        return solar_soc, False

    stale = gap_s > gap_threshold_s and soc_now != soc_before
    delta = soc_now - soc_before
    raw = solar_soc

    if delta > 0:
        # Bank only the solar PROPORTION. The rest dilutes, exactly as a pure
        # grid charge always did -- see docstring.
        raw += delta * max(0.0, min(1.0, solar_fraction))
    elif delta < 0 and soc_before > 0:
        raw -= (soc_before - soc_now) * (raw / soc_before)

    clamped = max(0.0, min(raw, soc_now))
    if clamped != raw:
        logger.warning(
            "solar ledger clamped %.3f -> %.3f (soc=%s): a sample was "
            "missed or the car charged somewhere unobserved",
            raw, clamped, soc_now)
    assert 0 <= clamped <= soc_now
    return clamped, stale


def banked_miles_rated(
    solar_soc: float, soc: int | None, range_mi: float | None
) -> float | None:
    """The car's own rated-range estimate applied to the banked fraction.

    Available immediately -- needs only the current soc and the car's own
    reported range, both on hand every tick, unlike pack_kwh/mi_per_kwh
    which need real history to earn any confidence. None when soc is
    unknown or zero (nothing to take a fraction of) or range_mi is unknown.
    """
    if soc is None or not soc or range_mi is None:
        return None
    return solar_soc / soc * range_mi


def banked_miles_measured(
    solar_soc: float, pack_kwh: float | None, mi_per_kwh: float | None
) -> float | None:
    """The owner's own measured consumption applied to the banked fraction.

    None until pack_kwh() and miles_per_kwh() have cleared their own
    thresholds -- see MIN_SESSIONS and MIN_MILES above. The more trustworthy
    of the two banked-miles figures once it exists; callers should prefer it
    over banked_miles_rated and say which one they are showing (spec 7.4:
    never blend the two, never switch without labelling).
    """
    if pack_kwh is None or mi_per_kwh is None:
        return None
    return solar_soc / 100 * pack_kwh * mi_per_kwh


# --------------------------------------------------------------------------
# Task 20: lifetime free miles driven. The banked-solar ledger above already
# knows, every tick, what FRACTION of the pack came from the sun -- this is
# not new physics, only accumulation: multiply that fraction by the miles
# actually driven since the last observation and keep a running lifetime
# total.
# --------------------------------------------------------------------------

def free_miles_step(
    free_miles_driven: float, tracked_miles: float, ledger_odo: float | None,
    odo_now: float, solar_soc_before: float, soc_before: int | None,
) -> tuple[float, float, float]:
    """Advance the lifetime free-miles ledger by one observation.

    Returns (new_free_miles_driven, new_tracked_miles, new_ledger_odo). Pure:
    no clock, no database -- the caller supplies the previous odometer
    reading (persisted as solar_state.ledger_odo, since there is no reliable
    way to re-derive "the previous sample" inside a pure function) and gets
    back the updated lifetime totals plus the new odometer baseline.

    THE ARITHMETIC (the brief's own formula, unchanged)::

        solar_share    = solar_soc_before / soc_before
        free_miles    += miles_in_window * solar_share
        tracked_miles += miles_in_window

    solar_soc_before and soc_before are deliberately the SAME "before" values
    ledger_step (above) is called with this same tick -- the banked ledger's
    own state prior to folding in this observation, not the value it
    produces after. The fraction is only valid for the pack as it stood
    across the window just driven; reading the post-update fraction here
    would credit this window with a solar share the pack could not actually
    have delivered across it.

    ledger_odo is None on the very first tick after this column existed --
    a fresh install, or the tick right after this feature was deployed.
    There is no previous odometer to diff against, so the only honest move
    is to record it and accumulate nothing: never assume driving happened
    before anything was watching (same doctrine as ledger_step's
    soc_before is None branch above). NEVER backfill this from history --
    the ledger starts at zero for a reason, and inventing a past would make
    every number after it unauditable.

    A negative delta cannot physically happen -- the odometer only counts
    up -- so one means a corrupt or reordered sample, not a car that
    reversed its own lifetime mileage. Silently subtracting it would make
    the total wrong in a way nobody could audit; the honest move is to
    ignore the WHOLE observation (log it, change nothing, including the
    baseline) and let the next good sample re-establish it -- exactly like
    a skipped tick, the miles are merged into whichever later window
    finally reads a consistent odometer again, never lost and never
    invented.

    soc_before of 0 or None means the fraction itself cannot be computed --
    0 as a literal (nothing left to take a share of) and None because the
    banked-solar ledger has not observed its own first tick yet (see
    ledger_step). Either way the odometer reading itself is still
    trustworthy (unlike the negative-delta case above), so the baseline
    still advances to odo_now -- only the window's contribution to both
    totals is skipped, rather than dividing by a number that means nothing.
    """
    if ledger_odo is None:
        return free_miles_driven, tracked_miles, odo_now

    miles_in_window = odo_now - ledger_odo
    if miles_in_window < 0:
        logger.warning(
            "odometer went backward %.3f -> %.3f: ignoring this window, "
            "not advancing the ledger baseline", ledger_odo, odo_now)
        return free_miles_driven, tracked_miles, ledger_odo

    if not soc_before:
        return free_miles_driven, tracked_miles, odo_now

    solar_share = solar_soc_before / soc_before
    return (free_miles_driven + miles_in_window * solar_share,
            tracked_miles + miles_in_window,
            odo_now)


# A 2022 Model S Long Range. Used ONLY to turn watts into miles for the live
# ticker before pack_kwh() has earned a measured figure, and always reported
# with basis="estimated" so the UI can say so. Never used for the ledger
# itself, which stays in SoC space precisely to avoid depending on this.
NOMINAL_PACK_KWH = 100.0


def accrual_mi_per_s(free_w: float, mi_per_kwh: float | None,
                     soc: int | None, range_mi: float | None,
                     pack: float | None) -> tuple[float, str]:
    """How fast banked free miles are growing, in miles per second.

    Returns (rate, basis) where basis is "measured", "rated" or "none".

    The UI animates between polls from this, so it must describe only the
    SOLAR part of the car's draw: `free_w` is the caller's job to compute as
    the draw minus any grid import. A car riding out a cloud at the floor is
    charging, but not on sunshine, and must not tick the counter up.

    mi/kWh comes from the owner's own measured consumption when that exists.
    Before then it is derived from the car's own rated range -- full rated
    range divided by pack size -- which needs a pack figure, hence the
    nominal. That is exactly the dependency the SoC-space ledger avoids, so
    it lives here at the display edge and nowhere else.
    """
    if free_w <= 0:
        return 0.0, "none"
    mpk, basis = effective_mi_per_kwh(mi_per_kwh, soc, range_mi, pack)
    if mpk is None:
        return 0.0, "none"
    return free_w / 1000.0 / 3600.0 * mpk, basis


def effective_mi_per_kwh(mi_per_kwh: float | None, soc: int | None,
                         range_mi: float | None,
                         pack: float | None) -> tuple[float | None, str]:
    """The mi/kWh to convert energy into miles with, and which basis it is.

    One resolver, used by every energy-to-miles conversion on the card -- the
    live ticker and the lifetime solar/grid totals -- so they can never
    disagree about how far a kilowatt-hour goes.

    Prefers the owner's own measured consumption. Falls back to the car's
    rated range over a pack size, which is the only place in this project
    that needs a pack figure at all; the ledger itself stays in SoC space
    precisely to avoid it. Returns (None, "none") rather than guessing.
    """
    if mi_per_kwh and mi_per_kwh > 0:
        return mi_per_kwh, "measured"
    if not soc or range_mi is None:
        return None, "none"
    mpk = (range_mi / soc * 100.0) / (pack or NOMINAL_PACK_KWH)
    return (mpk, "rated") if mpk > 0 else (None, "none")
