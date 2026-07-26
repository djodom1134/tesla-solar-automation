"""Replay real metered history through the controller, offline.

WHAT THIS PROVES AND WHAT IT DOES NOT.

calendar_history?kind=energy returns watt-HOURS per 5-minute bucket; the
controller consumes instantaneous signed watts. Converting gives the AVERAGE
over each bucket, which smooths away sub-5-minute cloud transients. The backtest
therefore UNDERSTATES stop/start churn -- treat its cycle count as a floor, not
an estimate.

Only days when the car was away are valid inputs, because the buckets contain
the car's own historical draw and replaying a day it charged at home
double-counts that energy and produces a fictional result. Known-clean days on
this account: 2026-07-14, 2026-07-15, 2026-07-21.

The simulation adds the car's modelled draw back onto the historical grid
reading, because the meter never saw it -- the whole day's history was
recorded with the car absent.

A SECOND, RELATED CAVEAT: the state machine is driven at the bucket's native
300-second cadence, not solar_config's default 120-second period_s that will
actually be deployed. ramp_a is amps-PER-TICK, and the dwell/hysteresis
thresholds are enforced in whole ticks (see solar.Policy), so a slower tick
means a slower real-time ramp and a longer real-time dwell than the shipped
config will exhibit. There is no data finer than 5 minutes to simulate at the
real cadence. In practice this shows up as the controller occasionally
overshooting into a few hundred Wh of import for one or two ticks after a
cloud edge, while it catches up -- expected of an integral controller reacting
to a coarse, averaged signal, not a bug in this harness, and plausibly slower
to correct here than the faster-cadence deployed controller would be.

This is a developer tool, not part of the app: nothing in tesla_automation
imports it, and it never touches solar_config or solar_state in car.db. It
does not enable the controller and does not require a home location.

Usage:
    .venv/bin/python tools/backtest.py 2026-07-21
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import solar
from config import settings
from tesla import TeslaClient

BUCKET_S = 300
CLEAN_DAYS = ("2026-07-14", "2026-07-15", "2026-07-21")


def grid_watts(b: dict) -> float:
    """One 5-minute energy bucket -> average signed watts. Positive is import.

    This is an AVERAGE over the bucket, not a sample: a cloud that passes in
    90 seconds and clears is invisible here, smoothed into the other 3.5
    minutes of sun. Any stop/start count derived from these averages is
    therefore a floor on the real controller's churn, not an estimate of it.
    """
    net_wh = (float(b.get("grid_energy_imported") or 0)
              - float(b.get("grid_energy_exported_from_solar") or 0)
              - float(b.get("grid_energy_exported_from_battery") or 0)
              - float(b.get("grid_energy_exported_from_generator") or 0))
    return net_wh * 3600.0 / BUCKET_S


def simulate(buckets: list[dict], cfg: dict) -> dict:
    """Replay one day of 5-minute buckets through the real control law and
    state machine (solar.control / solar.advance), offline.

    `cfg` overrides solar.CONFIG_DEFAULTS for this run only; `enabled` is
    forced on so the (still globally disabled) state machine actually
    engages. Nothing here writes to car.db -- solar_config.enabled on disk is
    untouched.
    """
    conf = dict(solar.CONFIG_DEFAULTS)
    conf.update(cfg or {})
    conf["enabled"] = 1
    tun = solar.tunables_from(conf, amps_max=48, volts=240)
    pol = solar.policy_from(conf)

    machine = solar.Machine(state="idle")
    amps = tun.min_a
    captured_wh = imported_wh = 0.0
    stop_starts = 0

    for b in buckets:
        house_grid_w = grid_watts(b)
        car_w = amps * tun.volts if machine.state in ("charging", "grace") else 0.0
        # The car's draw is added back, because the historical meter never saw it.
        grid_w = house_grid_w + car_w
        decision = solar.control(grid_w, amps, tun)
        tick = solar.Tick(
            surplus_w=solar.surplus_watts(car_w, grid_w), decision=decision,
            location="home", plugged=True, period_s=BUCKET_S)
        machine, actions = solar.advance(machine, tick, pol, tun)

        if "charge_stop" in actions:
            stop_starts += 1
        if "set_amps" in actions:
            amps = tun.min_a if machine.state == "grace" else decision.target_a
        if machine.state in ("charging", "grace"):
            drawn_wh = amps * tun.volts * BUCKET_S / 3600.0
            captured_wh += drawn_wh
            if grid_w > 0:
                imported_wh += grid_w * BUCKET_S / 3600.0

    return {"captured_kwh": round(captured_wh / 1000.0, 3),
            "imported_wh": round(imported_wh, 1),
            "stop_starts": stop_starts, "ticks": len(buckets)}


async def _fetch(day: str) -> list[dict]:
    """One calendar_history?kind=energy request for the given local day.

    This is a data request, not a command: it cannot wake the car and does
    not touch it. One call per day, budgeted -- do not loop this while
    debugging.
    """
    tz = ZoneInfo(settings.timezone)
    end = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=tz)
    client = TeslaClient(settings)
    try:
        sites = await client.energy_sites()
        if not sites:
            return []
        hist = await client.calendar_history(
            sites[0]["energy_site_id"], "day", end, settings.timezone)
        return (hist or {}).get("time_series") or []
    finally:
        await client.aclose()


def main() -> int:
    days = sys.argv[1:] or list(CLEAN_DAYS)
    print("NOTE: bucket averaging smooths sub-5-minute transients, so "
          "stop_starts below is a FLOOR on real controller churn, not an "
          "estimate of it.")
    for day in days:
        if day not in CLEAN_DAYS:
            print(f"WARNING {day} is not a known car-away day; "
                  "results include the car's own historical draw")
        buckets = asyncio.run(_fetch(day))
        if not buckets:
            print(f"{day}: no data")
            continue
        out = simulate(buckets, {})
        print(f"{day}: captured {out['captured_kwh']} kWh, "
              f"imported {out['imported_wh']} Wh, "
              f"{out['stop_starts']} stop/starts over {out['ticks']} buckets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
