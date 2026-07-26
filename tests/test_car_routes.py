from fastapi.testclient import TestClient

import app as app_module


def test_ranges_cover_the_ui_options():
    from car_routes import RANGES
    assert set(RANGES) == {"24h", "7d", "30d", "90d", "all"}
    assert RANGES["24h"] == 86400
    assert RANGES["7d"] == 7 * 86400
    assert RANGES["all"] == 0


def test_demo_state_is_live_and_shaped():
    client = TestClient(app_module.app)
    body = client.get("/api/car/state").json()
    assert body["car_state"] == "online"
    assert body["source"] == "live"
    assert body["view"]["soc"] is not None
    assert body["view"]["name"]


def test_demo_history_has_rows_and_a_gap():
    client = TestClient(app_module.app)
    body = client.get("/api/car/history?range=7d").json()
    assert len(body["rows"]) > 10
    assert any(r["gap"] for r in body["rows"]), "demo history must exercise sleep gaps"
    assert all("soc" in r and "ts" in r for r in body["rows"])


def test_history_rejects_an_unknown_range():
    client = TestClient(app_module.app)
    assert client.get("/api/car/history?range=nope").status_code == 400
