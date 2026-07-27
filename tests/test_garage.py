"""garage.py: the ratgdo decision function and its two HTTP calls.

status() and open() are proven against a fake transport (httpx.MockTransport)
only. Nothing here ever talks to the real device at 192.168.87.78, and
open() is never exercised against anything but a fake -- there is no test
that could accidentally POST to a real garage door.
"""
from __future__ import annotations

import httpx

import garage

BASE = dict(door_state="Closed", obstructed=False, armed=True,
            inside_ring_now=True, inside_ring_prev=False, shift="D")

BASE_URL = "http://192.168.87.78"


# -- should_open: pure decision function -------------------------------------

def test_opens_on_arrival_when_everything_is_right():
    assert garage.should_open(**BASE) is True


def test_never_opens_a_door_that_is_not_closed():
    """Only "Closed" is safe, and only "Closed" has been observed on the real
    device. Every other value -- including one we have never seen -- means do
    nothing."""
    for state in ("Open", "Opening", "Closing", "Stopped", "Obstructed", "", None, "closed"):
        assert garage.should_open(**{**BASE, "door_state": state}) is False, state


def test_never_opens_when_obstructed():
    assert garage.should_open(**{**BASE, "obstructed": True}) is False


def test_requires_the_latch_to_be_armed():
    assert garage.should_open(**{**BASE, "armed": False}) is False


def test_requires_a_transition_into_the_ring_not_mere_presence():
    """True all night while the car sleeps in the driveway; the transition is
    what means 'just arrived'."""
    assert garage.should_open(**{**BASE, "inside_ring_prev": True}) is False


def test_requires_the_car_to_be_moving():
    for shift in ("P", None, ""):
        assert garage.should_open(**{**BASE, "shift": shift}) is False


def test_refuses_when_not_inside_the_ring_at_all():
    """Neither "was inside" nor "is inside" -- there is no arrival to react
    to, transition-shaped or otherwise."""
    assert garage.should_open(**{**BASE, "inside_ring_now": False}) is False


# -- status(): GET /status.json ----------------------------------------------

def _transport(handler):
    return httpx.MockTransport(handler)


def test_status_returns_the_parsed_payload_on_success():
    payload = {"garageDoorState": "Closed", "garageObstructed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/status.json"
        return httpx.Response(200, json=payload)

    assert garage.status(BASE_URL, transport=_transport(handler)) == payload


def test_status_returns_none_on_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert garage.status(BASE_URL, transport=_transport(handler)) is None


def test_status_returns_none_on_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    assert garage.status(BASE_URL, transport=_transport(handler)) is None


def test_status_returns_none_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    assert garage.status(BASE_URL, transport=_transport(handler)) is None


def test_status_returns_none_on_malformed_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all {")

    assert garage.status(BASE_URL, transport=_transport(handler)) is None


def test_status_returns_none_when_payload_is_not_a_dict():
    """A JSON body that parses fine but is a list, not an object -- a caller
    must never have to distinguish this from "the door is fine"."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["Closed", False])

    assert garage.status(BASE_URL, transport=_transport(handler)) is None


# -- open(): POST /setgdo -----------------------------------------------------

def test_open_posts_form_encoded_garage_door_state_1():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.read().decode()
        return httpx.Response(200, text="OK")

    assert garage.open(BASE_URL, transport=_transport(handler)) is True
    assert seen["method"] == "POST"
    assert seen["path"] == "/setgdo"
    assert "application/x-www-form-urlencoded" in seen["content_type"]
    assert seen["body"] == "garageDoorState=1"


def test_open_returns_false_on_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert garage.open(BASE_URL, transport=_transport(handler)) is False


def test_open_returns_false_on_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    assert garage.open(BASE_URL, transport=_transport(handler)) is False


def test_open_returns_false_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    assert garage.open(BASE_URL, transport=_transport(handler)) is False


# -- close(): POST /setgdo, garageDoorState=0 --------------------------------
#
# Task 17b. Exactly as blunt as open() -- no warning of its own. Nothing here
# ever talks to the real device: every case below is proven against a fake
# transport, same discipline as open()'s tests above.

def test_close_posts_form_encoded_garage_door_state_0():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.read().decode()
        return httpx.Response(200, text="OK")

    assert garage.close(BASE_URL, transport=_transport(handler)) is True
    assert seen["method"] == "POST"
    assert seen["path"] == "/setgdo"
    assert "application/x-www-form-urlencoded" in seen["content_type"]
    assert seen["body"] == "garageDoorState=0"


def test_close_returns_false_on_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert garage.close(BASE_URL, transport=_transport(handler)) is False


def test_close_returns_false_on_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    assert garage.close(BASE_URL, transport=_transport(handler)) is False


def test_close_returns_false_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    assert garage.close(BASE_URL, transport=_transport(handler)) is False


# -- light_on(): POST /setgdo, garageLightOn=1 -------------------------------
#
# The visual half of the warning UL 325 / 16 CFR 1211 want for an unattended
# close -- see garage.py's module docstring for why there is no audible half
# reachable over this API.

def test_light_on_posts_form_encoded_garage_light_on_1():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read().decode()
        return httpx.Response(200, text="OK")

    assert garage.light_on(BASE_URL, transport=_transport(handler)) is True
    assert seen["path"] == "/setgdo"
    assert seen["body"] == "garageLightOn=1"


def test_light_on_returns_false_on_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert garage.light_on(BASE_URL, transport=_transport(handler)) is False


def test_light_on_returns_false_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    assert garage.light_on(BASE_URL, transport=_transport(handler)) is False


# -- safe_to_close(): pure decision -------------------------------------------
#
# Called twice by collector.py's scheduled close: once before the warning,
# once after the wait. The second call is the one that actually matters.

def test_safe_to_close_true_only_when_open_and_not_obstructed():
    assert garage.safe_to_close("Open", False) is True


def test_safe_to_close_false_when_obstructed_even_if_open():
    assert garage.safe_to_close("Open", True) is False


def test_safe_to_close_false_on_anything_other_than_exactly_open():
    """Fail closed on every value that is not exactly "Open", including one
    never observed on the real device -- the same philosophy as
    should_open()'s exact match on "Closed"."""
    for state in ("Closed", "Opening", "Closing", "Stopped", "", None, "open"):
        assert garage.safe_to_close(state, False) is False, state
