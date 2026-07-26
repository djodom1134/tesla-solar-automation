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


def test_home_is_null_until_set(client):
    body = client.get("/api/car/home").json()
    assert body["home"] is None
    assert body["classification"] == "unknown"


def test_home_round_trips(client):
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


def test_status_reports_idle_before_anything_runs(client):
    body = client.get("/api/car/solar/status").json()
    assert body["state"] == "idle"
    assert body["grace_import_wh_total"] == 0
