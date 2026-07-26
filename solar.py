"""Closed-loop solar charge control.

Everything here is pure: it takes numbers and returns numbers. collector.py
owns all I/O. That split is what makes a control system testable without a car,
a roof, or the weather.

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

from dataclasses import dataclass

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


def control(grid_w: float, current_a: int, tun: Tunables) -> Decision:
    """One tick of the integral controller."""
    error_w = -grid_w - tun.margin_w
    raw_target = current_a + error_w / tun.volts
    step_a = int(round(_clamp(error_w / tun.volts, -tun.ramp_a, tun.ramp_a)))
    target_a = int(_clamp(current_a + step_a, tun.min_a, tun.max_a))
    return Decision(
        target_a=target_a,
        write=abs(error_w) >= tun.deadband_w,
        # Expressed in AMPS, not watts, so the floor derives from the measured
        # voltage instead of a hardcoded 1200 W -- and so it stays correct in
        # every state rather than only when the car is idle.
        floor_breach=raw_target < tun.min_a,
        error_w=error_w,
        raw_target=raw_target,
    )
