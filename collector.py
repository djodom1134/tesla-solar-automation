"""Adaptive state-of-charge collector.

Runs independently of the web app under launchd so history stays continuous
when the dashboard is closed.

Two rules make this cheap and safe:
  * Every paid vehicle_data call is gated on the cheap, sleep-safe state check,
    because a 408 from a sleeping car is billed like any other request.
  * It never wakes the car. On 2021+ vehicles polling does not prevent sleep --
    only commands do -- so this costs no vampire drain.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

import vehicle
from config import settings
from store import Store
from tesla import TeslaAPIError, TeslaAuthError, TeslaClient, VehicleAsleep

DRIVING = {"D", "R", "N"}


def next_interval(car_state: str, view: dict | None, cfg) -> int:
    """Seconds until the next poll. Pure, so it is testable without a car."""
    if car_state != "online":
        return cfg.poll_asleep
    if view is None:
        return cfg.poll_idle
    # Driving changes SoC fastest, and a drive is short -- check it first so a
    # car that is both moving and (briefly) charging still samples densely.
    if view.get("shift") in DRIVING:
        return cfg.poll_driving
    if view.get("charging"):
        return cfg.poll_charging
    return cfg.poll_idle


async def poll_once(client: TeslaClient, store: Store, vin: str, cfg):
    """One cycle: cheap state check, then a paid read only if it can succeed."""
    try:
        car_state = (await client.vehicle(vin)).get("state") or "offline"
    except TeslaAPIError as exc:
        _log(f"state check failed: {exc}")
        return "offline", None

    if car_state != "online":
        return car_state, None

    try:
        view = vehicle.derive(await client.vehicle_data(vin))
    except VehicleAsleep:
        # Documented: vehicle_data can 408 even when /vehicles says online.
        return "asleep", None
    except TeslaAPIError as exc:
        _log(f"vehicle_data failed: {exc}")
        return car_state, None

    store.record(view)
    return car_state, view


async def run(once: bool = False) -> int:
    client = TeslaClient(settings)
    store = Store(settings.db_file)
    try:
        vin = await client.resolve_vin()
    except (TeslaAPIError, TeslaAuthError) as exc:
        _log(f"cannot resolve VIN: {exc}")
        await client.aclose()
        store.close()
        return 1

    _log(f"collecting for {vin}")
    try:
        while True:
            try:
                car_state, view = await poll_once(client, store, vin, settings)
            except TeslaAuthError as exc:
                # Nothing to retry against; launchd will restart us later.
                _log(f"auth lost: {exc}")
                return 1
            soc = (view or {}).get("soc")
            _log(f"{car_state}" + (f" soc={soc}%" if soc is not None else ""))
            if once:
                return 0
            await asyncio.sleep(next_interval(car_state, view, settings))
    finally:
        await client.aclose()
        store.close()


def _log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    sys.exit(asyncio.run(run(once=parser.parse_args().once)))
