from fastapi.testclient import TestClient

import app as app_module
import car_routes
import demo
import store
from tesla import TeslaAuthError


class _FakeStore:
    """Stands in for car_routes.store() so /health tests never touch car.db."""

    def snapshot(self, vin):
        return None

    def count_since(self, ts):
        return 0


class _FakeClientNotAuthenticated:
    async def resolve_vin(self):
        return "5YJSA00000F000000"

    async def _access_token(self):
        raise TeslaAuthError("not logged in")

    async def fleet_status(self, vins):
        return {"key_paired_vins": []}


class _FakeClientBadToken:
    """Returns a token that isn't a decodable JWT -- a decode failure,
    distinct from never having a token at all."""

    async def resolve_vin(self):
        return "5YJSA00000F000000"

    async def _access_token(self):
        return "not-a-valid-jwt"

    async def fleet_status(self, vins):
        return {"key_paired_vins": []}


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


def test_health_reports_not_authenticated_distinctly(monkeypatch):
    """Not logged in must not look like a decode failure or like zero scopes
    for any other reason -- an operator needs to tell these apart."""
    monkeypatch.setattr(car_routes, "DEMO", False)
    monkeypatch.setattr(car_routes, "store", lambda: _FakeStore())
    monkeypatch.setattr(car_routes, "_proxy_up", lambda: False)
    monkeypatch.setattr(car_routes, "_client", lambda: _FakeClientNotAuthenticated())

    client = TestClient(app_module.app)
    body = client.get("/api/car/health").json()
    assert body["auth_error"] == "not_authenticated"
    assert body["scopes"] == []
    assert body["missing_scopes"] == []


def test_health_reports_scopes_decode_failure_distinctly(monkeypatch):
    """A present-but-undecodable token is a different failure than not being
    logged in at all -- it must not collapse to the same state."""
    monkeypatch.setattr(car_routes, "DEMO", False)
    monkeypatch.setattr(car_routes, "store", lambda: _FakeStore())
    monkeypatch.setattr(car_routes, "_proxy_up", lambda: False)
    monkeypatch.setattr(car_routes, "_client", lambda: _FakeClientBadToken())

    client = TestClient(app_module.app)
    body = client.get("/api/car/health").json()
    assert body["auth_error"] == "scopes_decode_failed"
    assert body["scopes"] == []


def test_demo_gap_threshold_tracks_store_gap_seconds(monkeypatch):
    """soc_history must read store.GAP_SECONDS live, not a hardcoded copy,
    or the demo fixture can silently drift from the store's real definition
    of a sleep gap."""
    monkeypatch.setattr(store, "GAP_SECONDS", 100)  # well under the 300s sample step
    rows = demo.soc_history(1, "America/Denver")
    assert len(rows) > 2
    # Every consecutive pair is at least 300s apart, which now exceeds the
    # patched threshold -- every row after the first must read as a gap.
    # This only holds if soc_history looks up store.GAP_SECONDS at call time.
    assert all(r["gap"] for r in rows[1:])
