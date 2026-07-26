from __future__ import annotations

import os

os.environ["DEMO"] = "1"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
