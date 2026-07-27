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

from typing import Any

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


def solar_kwh(ticks: list[dict[str, Any]]) -> float:
    """Solar energy delivered to the car, from the controller's own tick log.

    The controller is the only thing modulating the car's draw, so within an
    engaged tick the attribution is exact rather than pro-rata: whatever the
    car drew, minus whatever was being imported at that instant, came from
    the sun::

        solar_w = max(0, car_w - max(grid_w, 0))

    Deliberately conservative -- import is charged wholly against the car
    even though some of it fed the house, and importing more than the car
    drew means none of it was solar. A lower bound is the right kind of
    wrong for a number whose entire purpose is to be trustworthy.
    """
    wh = 0.0
    for tick in ticks:
        if tick.get("state") not in ENGAGED_STATES:
            continue
        car_w = float(tick.get("car_w") or 0)
        grid_w = float(tick.get("grid_w") or 0)
        period_s = float(tick.get("period_s") or 0)
        solar_w = max(0.0, car_w - max(grid_w, 0.0))
        wh += solar_w * period_s / 3600
    return wh / 1000


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
