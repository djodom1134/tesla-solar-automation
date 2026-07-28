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


class _FakeStoreWithView:
    """Stands in for car_routes.store() with a controllable snapshot, to drive
    the trigger_homelink lat/lon injection without touching car.db."""

    def __init__(self, view):
        self._view = view

    def snapshot(self, vin):
        return None if self._view is None else {"ts": 0, "view": self._view}

    def count_since(self, ts):
        return 0


class _FakeCache:
    def clear(self):
        pass


class _FakeClientCommand:
    """Records what car_routes actually sent to TeslaClient.command()."""

    def __init__(self):
        self.calls = []
        self.cache = _FakeCache()

    async def resolve_vin(self):
        return "5YJSA00000F000000"

    async def command(self, vin, name, body):
        self.calls.append((vin, name, dict(body)))
        return 200, {"response": {"result": True, "reason": ""}}


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


def test_trigger_homelink_injects_lat_lon_from_the_snapshot(monkeypatch):
    """A needs_location command must reach TeslaClient.command() with lat/lon
    merged in from the last stored view, not sent empty."""
    monkeypatch.setattr(car_routes, "DEMO", False)
    monkeypatch.setattr(
        car_routes, "store",
        lambda: _FakeStoreWithView({"lat": 37.4, "lon": -122.1}),
    )
    fake_client = _FakeClientCommand()
    monkeypatch.setattr(car_routes, "_client", lambda: fake_client)

    client = TestClient(app_module.app)
    resp = client.post("/api/car/command/trigger_homelink", json={})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert len(fake_client.calls) == 1
    _, name, body = fake_client.calls[0]
    assert name == "trigger_homelink"
    assert body == {"lat": 37.4, "lon": -122.1}


def test_trigger_homelink_refuses_with_no_snapshot(monkeypatch):
    """No location on record must fail loudly with an actionable 400, and
    must never reach the proxy with a doomed empty body."""
    monkeypatch.setattr(car_routes, "DEMO", False)
    monkeypatch.setattr(car_routes, "store", lambda: _FakeStoreWithView(None))
    fake_client = _FakeClientCommand()
    monkeypatch.setattr(car_routes, "_client", lambda: fake_client)

    client = TestClient(app_module.app)
    resp = client.post("/api/car/command/trigger_homelink", json={})

    assert resp.status_code == 400
    assert "location" in resp.json()["detail"].lower()
    assert fake_client.calls == []


def test_trigger_homelink_refuses_when_lat_lon_are_none(monkeypatch):
    """Tesla omits latitude/longitude entirely (not null) when the
    vehicle_location scope is missing -- once stored that collapses to None,
    and it must be treated the same as no snapshot: refuse, don't guess."""
    monkeypatch.setattr(car_routes, "DEMO", False)
    monkeypatch.setattr(
        car_routes, "store",
        lambda: _FakeStoreWithView({"lat": None, "lon": None}),
    )
    fake_client = _FakeClientCommand()
    monkeypatch.setattr(car_routes, "_client", lambda: fake_client)

    client = TestClient(app_module.app)
    resp = client.post("/api/car/command/trigger_homelink", json={})

    assert resp.status_code == 400
    assert "location" in resp.json()["detail"].lower()
    assert fake_client.calls == []


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


def test_app_javascript_is_served_no_store():
    """ES modules are cached hard, and the cache key ignores the query string
    on the importing PAGE -- so a hard reload of car.html can still execute a
    stale car.js. That produced two baffling sessions in this project where
    the server and the disk were byte-identical and correct while the page
    ran code from an earlier deploy.
    """
    from fastapi.testclient import TestClient
    import app as app_module

    with TestClient(app_module.app) as client:
        for path in ("/car.js", "/car.html", "/shared.js", "/styles.css"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert "no-store" in r.headers.get("cache-control", ""), (
                f"{path} must not be cached: a stale module is indistinguishable "
                "from a broken deploy")


def test_vendored_assets_keep_normal_caching():
    """Only the app's own files are no-store. Vendored libraries change when
    the file changes, which is approximately never, and re-fetching them on
    every load would be waste for no benefit."""
    from pathlib import Path
    from fastapi.testclient import TestClient
    import app as app_module

    vendor = Path(app_module.BASE_DIR) / "static" / "vendor"
    files = sorted(p for p in vendor.rglob("*") if p.is_file()) if vendor.is_dir() else []
    if not files:
        pytest.skip("no vendored assets on this install")
    rel = files[0].relative_to(vendor.parent)
    with TestClient(app_module.app) as client:
        r = client.get("/" + str(rel))
        assert r.status_code == 200
        assert "no-store" not in r.headers.get("cache-control", "")
