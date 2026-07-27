"""ratgdo garage control: read the opener's own sensors over the LAN, and
open the door -- once -- when the car arrives home already moving.

Spec S1.8 recorded garage state as unreadable through Tesla: trigger_homelink
is a blind one-way toggle with no state anywhere in vehicle_data, so "open
the door if it isn't already open" could not be built. A ratgdo
(homekit-ratgdo firmware) reads the opener's own door/obstruction/lock
sensors and serves them at /status.json, entirely outside the Tesla API.

Verified live against the owner's device at http://192.168.87.78/:

    garageDoorState : "Closed"     <- a STRING, not an enum int
    garageObstructed: false        obstruction sensor wired (pinBasedObst)
    garageLockState : "Enabled"
    paired          : true         firmware 2.1.6, passwordRequired: false

THE RULE THAT MATTERS MOST: act only when garageDoorState is exactly
"Closed". "Closed" is the only value ever observed on the real device and
the only one proven safe. Every other value -- "Open", "Opening", "Closing",
"Stopped", something never seen before, a lowercase variant, an empty
string, None, a malformed payload, an unreachable device -- means do
nothing. A firmware update that renames a state must make this feature
*stop working*, not start guessing at an enum.

OPEN ONLY. There is deliberately no should_close and no close(). Closing is
the one irreversible direction -- it can trap a person, a pet, or a bicycle
-- and the owner has confirmed open-only.
"""
from __future__ import annotations

import httpx

# The same driving-shift set collector.py already gates poll_driving on
# (collector.DRIVING). "The car is near home all night while it sleeps in
# the driveway" is not motion; only D/R/N -- and a transition into the ring
# -- means the car just arrived.
DRIVING_SHIFTS = {"D", "R", "N"}


def should_open(
    door_state,
    obstructed: bool,
    armed: bool,
    inside_ring_now: bool,
    inside_ring_prev: bool,
    shift,
) -> bool:
    """Pure decision, no I/O. True only when every one of these holds:

    1. door_state is exactly "Closed" -- fail closed on anything else,
       including values never observed on the real device.
    2. obstructed is false -- something is in the doorway otherwise.
    3. armed is true -- the one-shot latch has not already fired since it
       was last armed on leaving the ring.
    4. The car is transitioning INTO the ring right now (inside_ring_now and
       not inside_ring_prev) -- mere presence is not arrival.
    5. shift is a driving shift (D/R/N) -- the car must be moving, not
       parked-but-technically-inside-the-ring.
    """
    if door_state != "Closed":
        return False
    if obstructed:
        return False
    if not armed:
        return False
    if inside_ring_prev or not inside_ring_now:
        return False
    if shift not in DRIVING_SHIFTS:
        return False
    return True


def status(
    base_url: str,
    timeout: float = 4.0,
    transport: httpx.BaseTransport | None = None,
) -> dict | None:
    """GET {base_url}/status.json.

    Returns None on every failure mode -- connection error, timeout,
    non-200, malformed JSON, or a payload that isn't a JSON object -- so a
    caller never has to tell "the door is fine" apart from "I could not
    ask". `transport` exists only so tests can substitute an
    httpx.MockTransport; production callers never pass it.
    """
    url = f"{base_url.rstrip('/')}/status.json"
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            resp = client.get(url)
    except httpx.HTTPError:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    if not isinstance(data, dict):
        return None

    return data


def open(
    base_url: str,
    timeout: float = 4.0,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """POST garageDoorState=1, form-encoded, to {base_url}/setgdo.

    Returns True only if the request completed and the device answered 200.
    No retries: a retry on an open is harmless but pointless, and it is the
    caller's job to re-read status() afterward -- that verification read is
    what actually tells the truth, not this return value. `transport` exists
    only for tests; production callers never pass it.

    Named `open`, shadowing the builtin within this module, to match the
    interface the brief specifies; this module does no file I/O and never
    needs the builtin.
    """
    url = f"{base_url.rstrip('/')}/setgdo"
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            resp = client.post(url, data={"garageDoorState": 1})
    except httpx.HTTPError:
        return False

    return resp.status_code == 200
