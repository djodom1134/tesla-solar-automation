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
