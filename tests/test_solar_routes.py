from __future__ import annotations

import os

os.environ["DEMO"] = "1"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import garage
import solar_routes


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "db_file", tmp_path / "t.db")
    solar_routes._store = None
    app = FastAPI()
    app.include_router(solar_routes.router)
    return TestClient(app)


def test_home_is_null_until_set(client, monkeypatch):
    # Real (store-backed) path -- see the comment on
    # test_status_reports_idle_before_anything_runs for why this override
    # is needed despite the module-level DEMO default.
    monkeypatch.setattr(solar_routes, "DEMO", False)
    body = client.get("/api/car/home").json()
    assert body["home"] is None
    assert body["classification"] == "unknown"


def test_home_round_trips(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    r = client.put("/api/car/home",
                   json={"latitude": 40.1672, "longitude": -105.1019, "radius_m": 120})
    assert r.status_code == 200
    body = client.get("/api/car/home").json()
    assert body["home"]["radius_m"] == 120
    assert body["home"]["latitude"] == pytest.approx(40.1672)


def test_home_rejects_out_of_range_coordinates(client):
    assert client.put("/api/car/home",
                      json={"latitude": 91, "longitude": 0, "radius_m": 100}).status_code == 400
    assert client.put("/api/car/home",
                      json={"latitude": 0, "longitude": 181, "radius_m": 100}).status_code == 400


def test_home_rejects_a_silly_radius(client):
    assert client.put("/api/car/home",
                      json={"latitude": 40, "longitude": -105, "radius_m": 5}).status_code == 400
    assert client.put("/api/car/home",
                      json={"latitude": 40, "longitude": -105, "radius_m": 99999}).status_code == 400


def test_config_defaults_then_partial_update(client):
    cfg = client.get("/api/car/solar/config").json()
    assert cfg["enabled"] == 0
    assert cfg["period_s"] == 120
    client.put("/api/car/solar/config", json={"enabled": 1, "soc_ceiling": 95})
    cfg = client.get("/api/car/solar/config").json()
    assert cfg["enabled"] == 1
    assert cfg["soc_ceiling"] == 95
    assert cfg["period_s"] == 120


def test_config_rejects_unknown_fields(client):
    r = client.put("/api/car/solar/config", json={"nonsense": 1})
    assert r.status_code == 400


def test_config_rejects_an_out_of_range_ceiling(client):
    assert client.put("/api/car/solar/config", json={"soc_ceiling": 49}).status_code == 400
    assert client.put("/api/car/solar/config", json={"soc_ceiling": 101}).status_code == 400


def test_config_rejects_a_period_below_the_meter_refresh(client):
    """grid_power refreshes at 60s. A faster loop reads the same number twice."""
    assert client.put("/api/car/solar/config", json={"period_s": 30}).status_code == 400


def test_config_updates_survive_a_later_unrelated_put(client):
    """The guarantee the setup page depends on: changing one setting must not
    silently revert another. A handler that rebuilt the payload from
    CONFIG_DEFAULTS on every write would pass every other test in this file."""
    client.put("/api/car/solar/config", json={"soc_ceiling": 100})
    client.put("/api/car/solar/config", json={"grace_s": 300})
    cfg = client.get("/api/car/solar/config").json()
    assert cfg["soc_ceiling"] == 100, "second PUT reverted the first"
    assert cfg["grace_s"] == 300
    assert cfg["period_s"] == 120, "untouched field should still be default"


def test_deadline_fields_accept_an_explicit_null(client):
    """Clearing a deadline is how the owner turns the warning off, so null is
    a legitimate value -- but ONLY for these two fields."""
    assert client.put("/api/car/solar/config",
                      json={"deadline_soc": 70, "deadline_hour": 7}).status_code == 200
    assert client.put("/api/car/solar/config",
                      json={"deadline_soc": None}).status_code == 200
    cfg = client.get("/api/car/solar/config").json()
    assert cfg["deadline_soc"] is None
    assert cfg["deadline_hour"] == 7, "clearing one must not clear the other"


def test_other_fields_reject_an_explicit_null(client):
    assert client.put("/api/car/solar/config",
                      json={"soc_ceiling": None}).status_code == 400
    assert client.put("/api/car/solar/config",
                      json={"period_s": None}).status_code == 400


def test_status_reports_idle_before_anything_runs(client, monkeypatch):
    # This test exercises the real (store-backed) status path. The module-level
    # DEMO flag defaults to True across the whole suite (tests/conftest.py sets
    # DEMO=1 globally) -- without this override the DEMO short-circuit added
    # below would return the fixed demo fixture instead of hitting the store,
    # same convention as test_car_routes.py's `monkeypatch.setattr(..., "DEMO", False)`.
    monkeypatch.setattr(solar_routes, "DEMO", False)
    body = client.get("/api/car/solar/status").json()
    assert body["state"] == "idle"
    assert body["grace_import_wh_total"] == 0


def test_status_pins_enabled_false_on_the_shipped_default(client, monkeypatch):
    """I12: the branch ships with solar_config.enabled = 0 and no tick has
    ever run. A disabled controller must render distinguishably from an
    enabled-but-quiet one (both report state:"idle") -- `enabled` is the
    only field that tells them apart. Pins that the real, unconfigured
    payload carries `enabled: false` today."""
    monkeypatch.setattr(solar_routes, "DEMO", False)
    body = client.get("/api/car/solar/status").json()
    assert body["enabled"] is False


def test_status_enabled_reflects_config(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.put("/api/car/solar/config", json={"enabled": 1})
    body = client.get("/api/car/solar/status").json()
    assert body["enabled"] is True


def test_demo_status_reports_a_live_session(client):
    """DEMO=1 must render every field the card shows, without a car.

    This guards the payload *shape* (a fixture missing a key the card reads),
    not the DEMO short-circuit itself -- the real store-backed defaults satisfy
    key-presence and a valid `state` equally well. See
    test_demo_status_returns_the_fixture_not_real_data for the test that pins
    the short-circuit."""
    body = client.get("/api/car/solar/status").json()
    assert body["state"] in {"idle", "charging", "grace", "stopped"}
    for key in ("surplus_w", "amps", "soc", "grace_import_wh_today",
                "capped", "dirty", "raised_to", "original_limit"):
        assert key in body, key


def test_demo_and_real_status_have_the_same_key_set(client, monkeypatch):
    """I12: demo.solar_status() and the real, store-backed payload must carry
    the same fields, `enabled` included -- a demo fixture missing a key the
    card reads is a bug the DEMO=1 dogfood loop exists to catch before a real
    car does."""
    demo_body = client.get("/api/car/solar/status").json()
    monkeypatch.setattr(solar_routes, "DEMO", False)
    real_body = client.get("/api/car/solar/status").json()
    assert set(demo_body) == set(real_body)


def test_demo_status_returns_the_fixture_not_real_data(client):
    """Pins the DEMO short-circuit itself, not merely the payload shape.

    Key-presence and a valid `state` are satisfied by the real store-backed
    defaults too, so the previous test would still pass if the short-circuit
    were deleted. These values can only come from the fixture.
    """
    body = client.get("/api/car/solar/status").json()
    assert body["state"] == "charging"
    assert body["surplus_w"] == 6240.0
    assert body["raised_to"] == 90 and body["original_limit"] == 80
    assert body["grace_import_wh_today"] == 41.3


# --------------------------------------------------------------------------
# Task 17b -- garage HTTP surface. garage.status()/open()/close() are always
# monkeypatched here: nothing in this file may ever perform a real network
# call, so there is no path by which these tests could reach the owner's
# actual garage door.
# --------------------------------------------------------------------------

def test_garage_config_defaults(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    cfg = client.get("/api/car/garage/config").json()
    assert cfg == {
        "garage_url": None, "garage_auto_open": 0, "garage_ring_m": 800,
        "garage_close_hour": None, "garage_close_warn_s": 8,
    }


def test_garage_config_round_trips_and_survives_a_later_unrelated_put(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.put("/api/car/garage/config", json={
        "garage_url": "http://192.168.87.78", "garage_auto_open": 1,
    })
    client.put("/api/car/garage/config", json={"garage_ring_m": 400})
    cfg = client.get("/api/car/garage/config").json()
    assert cfg["garage_url"] == "http://192.168.87.78"
    assert cfg["garage_auto_open"] == 1, "second PUT reverted the first"
    assert cfg["garage_ring_m"] == 400


def test_garage_config_accepts_an_explicit_null_close_hour(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.put("/api/car/garage/config", json={"garage_close_hour": 22})
    r = client.put("/api/car/garage/config", json={"garage_close_hour": None})
    assert r.status_code == 200
    assert client.get("/api/car/garage/config").json()["garage_close_hour"] is None


def test_garage_config_clearing_the_url_with_an_empty_string_stores_null(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.put("/api/car/garage/config", json={"garage_url": "http://192.168.87.78"})
    client.put("/api/car/garage/config", json={"garage_url": ""})
    assert client.get("/api/car/garage/config").json()["garage_url"] is None


def test_garage_config_rejects_unknown_fields(client):
    assert client.put("/api/car/garage/config", json={"nonsense": 1}).status_code == 400


@pytest.mark.parametrize("field,value", [
    ("garage_url", "ftp://not-http"),
    ("garage_auto_open", 2),
    ("garage_ring_m", 10),
    ("garage_ring_m", 100_000),
    ("garage_close_hour", 24),
    ("garage_close_hour", -1),
    ("garage_close_warn_s", 1),
    ("garage_close_warn_s", 1000),
])
def test_garage_config_rejects_out_of_range_values(client, field, value):
    assert client.put("/api/car/garage/config", json={field: value}).status_code == 400


def test_get_garage_reports_unreachable_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    body = client.get("/api/car/garage").json()
    assert body == {"reachable": False}


def test_get_garage_returns_the_live_reading_when_configured(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.put("/api/car/garage/config", json={"garage_url": "http://192.168.87.78"})

    seen = {}
    def fake_status(url):
        seen["url"] = url
        return {"garageDoorState": "Closed", "garageObstructed": False, "garageLightOn": True}
    monkeypatch.setattr(garage, "status", fake_status)

    body = client.get("/api/car/garage").json()
    assert seen["url"] == "http://192.168.87.78"
    assert body == {"reachable": True, "door_state": "Closed",
                    "obstructed": False, "light_on": True}


def test_get_garage_reports_unreachable_when_status_returns_none(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.put("/api/car/garage/config", json={"garage_url": "http://192.168.87.78"})
    monkeypatch.setattr(garage, "status", lambda url: None)

    body = client.get("/api/car/garage").json()
    assert body == {"reachable": False}, "never show a stale state as current"


def test_post_garage_open_requires_a_configured_url(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    r = client.post("/api/car/garage/open")
    assert r.status_code == 400


def test_post_garage_open_commands_and_returns_the_fresh_reading(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.put("/api/car/garage/config", json={"garage_url": "http://192.168.87.78"})

    calls = []
    monkeypatch.setattr(garage, "open", lambda url: calls.append(url) or True)
    monkeypatch.setattr(garage, "status",
                        lambda url: {"garageDoorState": "Open", "garageObstructed": False,
                                     "garageLightOn": False})

    r = client.post("/api/car/garage/open")
    assert r.status_code == 200
    assert calls == ["http://192.168.87.78"]
    assert r.json() == {"ok": True, "reachable": True, "door_state": "Open",
                        "obstructed": False, "light_on": False}


def test_post_garage_close_requires_a_configured_url(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    assert client.post("/api/car/garage/close").status_code == 400


def test_post_garage_close_commands_and_returns_the_fresh_reading(client, monkeypatch):
    """The button is manual -- the owner is present -- so this must call
    garage.close() directly with no warning wait, unlike the scheduled
    close."""
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.put("/api/car/garage/config", json={"garage_url": "http://192.168.87.78"})

    calls = []
    monkeypatch.setattr(garage, "close", lambda url: calls.append(url) or True)
    monkeypatch.setattr(garage, "status",
                        lambda url: {"garageDoorState": "Closed", "garageObstructed": False,
                                     "garageLightOn": False})

    r = client.post("/api/car/garage/close")
    assert r.status_code == 200
    assert calls == ["http://192.168.87.78"]
    assert r.json()["door_state"] == "Closed"


def test_post_garage_test_requires_a_url(client):
    assert client.post("/api/car/garage/test", json={}).status_code == 400


def test_post_garage_test_reads_whatever_url_is_given_saved_or_not(client, monkeypatch):
    """The setup page's Test connection button: read-only, and must work
    against a URL that has not been saved to config yet."""
    seen = {}
    def fake_status(url):
        seen["url"] = url
        return {"garageDoorState": "Open", "garageObstructed": True, "garageLightOn": False}
    monkeypatch.setattr(garage, "status", fake_status)

    r = client.post("/api/car/garage/test", json={"url": "http://10.0.0.5"})
    assert seen["url"] == "http://10.0.0.5"
    assert r.json() == {"reachable": True, "door_state": "Open",
                        "obstructed": True, "light_on": False}


def test_post_garage_test_reports_unreachable_rather_than_raising(client, monkeypatch):
    monkeypatch.setattr(garage, "status", lambda url: None)
    r = client.post("/api/car/garage/test", json={"url": "http://10.0.0.5"})
    assert r.status_code == 200
    assert r.json() == {"reachable": False}


def test_demo_garage_endpoints_never_touch_the_real_device(client, monkeypatch):
    """DEMO=1 is the default in this whole suite (see conftest.py). GET
    /garage, and the open/close buttons, must all short-circuit to the
    fixture rather than ever calling garage.status/open/close -- a boom
    stand-in on each proves it."""
    def boom(*a, **k):
        raise AssertionError("DEMO must never touch the real device")
    monkeypatch.setattr(garage, "status", boom)
    monkeypatch.setattr(garage, "open", boom)
    monkeypatch.setattr(garage, "close", boom)

    assert client.get("/api/car/garage").json()["reachable"] is True
    assert client.post("/api/car/garage/open").json()["ok"] is True
    assert client.post("/api/car/garage/close").json()["ok"] is True
