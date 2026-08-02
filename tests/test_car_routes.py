import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as app_module
import auth
import car_routes
import demo
import store
from tesla import TeslaAuthError

TEST_TOKEN = "test-token"


@pytest.fixture(autouse=True)
def _api_token(monkeypatch):
    """auth.install() guards /api/* for any client that is not on loopback,
    and starlette 0.41's TestClient reports its host as "testclient". The
    `client=("127.0.0.1", …)` kwarg that would let us claim loopback does not
    exist in this version, so these tests present the token instead."""
    monkeypatch.setattr(auth.settings, "api_token", TEST_TOKEN)


def _client(app):
    return TestClient(app, headers={"X-Api-Key": TEST_TOKEN})


class _FakeStore:
    """Stands in for car_routes.store() so /health tests never touch car.db."""

    def latest_vin(self):
        return None

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

    def latest_vin(self):
        return None

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
    client = _client(app_module.app)
    body = client.get("/api/car/state").json()
    assert body["car_state"] == "online"
    assert body["source"] == "live"
    assert body["view"]["soc"] is not None
    assert body["view"]["name"]


def test_demo_history_has_rows_and_a_gap():
    client = _client(app_module.app)
    body = client.get("/api/car/history?range=7d").json()
    assert len(body["rows"]) > 10
    assert any(r["gap"] for r in body["rows"]), "demo history must exercise sleep gaps"
    assert all("soc" in r and "ts" in r for r in body["rows"])


def test_history_rejects_an_unknown_range():
    client = _client(app_module.app)
    assert client.get("/api/car/history?range=nope").status_code == 400


def test_health_reports_not_authenticated_distinctly(monkeypatch):
    """Not logged in must not look like a decode failure or like zero scopes
    for any other reason -- an operator needs to tell these apart."""
    monkeypatch.setattr(car_routes, "DEMO", False)
    monkeypatch.setattr(car_routes, "store", lambda: _FakeStore())
    monkeypatch.setattr(car_routes, "_proxy_up", lambda: False)
    monkeypatch.setattr(car_routes, "_client", lambda: _FakeClientNotAuthenticated())

    client = _client(app_module.app)
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

    client = _client(app_module.app)
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
    # A command now books against the daily cap, and _FakeStoreWithView has
    # no sqlite connection for count_request to write through.
    monkeypatch.setattr(car_routes, "_spend", lambda vin: None)

    client = _client(app_module.app)
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

    client = _client(app_module.app)
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

    client = _client(app_module.app)
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
    import app as app_module

    with _client(app_module.app) as client:
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
    import app as app_module

    vendor = Path(app_module.BASE_DIR) / "static" / "vendor"
    files = sorted(p for p in vendor.rglob("*") if p.is_file()) if vendor.is_dir() else []
    if not files:
        pytest.skip("no vendored assets on this install")
    rel = files[0].relative_to(vendor.parent)
    with _client(app_module.app) as client:
        r = client.get("/" + str(rel))
        assert r.status_code == 200
        assert "no-store" not in r.headers.get("cache-control", "")


class _CountingVinClient:
    """Counts resolve_vin() calls -- each one is a billed GET /api/1/vehicles
    when TESLA_VIN is unset."""

    def __init__(self):
        self.resolve_calls = 0

    async def resolve_vin(self):
        self.resolve_calls += 1
        return "5YJSA00000F000000"


def test_history_costs_no_tesla_request(monkeypatch, tmp_path):
    """/api/car/history reads the local samples table and nothing else -- but
    it went through _vin(), which called resolve_vin(), which falls through to
    GET /api/1/vehicles whenever TESLA_VIN is unset (it is). A free endpoint
    was paying a billed call on every load, and a dashboard left open on a
    wall tablet paid it every refresh.
    """
    monkeypatch.setattr(car_routes, "DEMO", False)
    fake = _CountingVinClient()
    monkeypatch.setattr(car_routes, "_client", lambda: fake)

    class _HistoryStore:
        """No real sqlite: TestClient dispatches the route on another thread,
        and a Connection cannot cross threads."""
        def first_sample(self, vin): return None
        def history(self, vin, start, end): return []

    monkeypatch.setattr(car_routes, "store", _HistoryStore)
    monkeypatch.setattr(car_routes.settings, "vin", "5YJSA00000F000000")

    with _client(app_module.app) as client:
        r = client.get("/api/car/history?range=24h")

    assert r.status_code == 200
    assert fake.resolve_calls == 0, (
        "a local history read must not spend a Tesla request resolving the VIN")


def test_vin_prefers_config_then_database_and_only_then_the_api(monkeypatch, tmp_path):
    """Three tiers, cheapest first. resolve_vin() is the last resort, not the
    first move."""
    import asyncio
    fake = _CountingVinClient()
    monkeypatch.setattr(car_routes, "DEMO", False)
    monkeypatch.setattr(car_routes, "_client", lambda: fake)

    rows = []
    class _SampleStore:
        def latest_vin(self): return rows[0] if rows else None
    monkeypatch.setattr(car_routes, "store", _SampleStore)

    # 1. configured -- free
    monkeypatch.setattr(car_routes.settings, "vin", "CONFIGURED_VIN")
    assert asyncio.run(car_routes._vin()) == "CONFIGURED_VIN"
    assert fake.resolve_calls == 0

    # 2. not configured, but a sample exists -- still free
    monkeypatch.setattr(car_routes.settings, "vin", "")
    rows.append("DBVIN")
    assert asyncio.run(car_routes._vin()) == "DBVIN"
    assert fake.resolve_calls == 0, "the database already knows the VIN"

    # 3. nothing configured, nothing recorded -- only NOW is the API justified
    rows.clear()
    assert asyncio.run(car_routes._vin()) == "5YJSA00000F000000"
    assert fake.resolve_calls == 1, "last resort, and only once"


def test_wake_counts_against_the_daily_cap_and_refuses_when_tripped(monkeypatch, tmp_path):
    """daily_request_cap guarded only the collector.

    solar.count_request() was called at collector.py:284 and nowhere else, so
    every billed HTTP route -- /wake at $0.02 a press, /command, /state --
    spent outside the cap entirely. requests_today reported a comfortable
    number while the budget drained through a door it did not watch. A
    backstop that does not count the most expensive request is not a backstop.
    """
    import solar
    from types import SimpleNamespace

    st = store.Store(tmp_path / "car.db")
    # solar.count_request uses `capped = count >= cap`, so the cap-th request
    # is the one refused, not the one after it. That fencepost is the
    # collector's existing contract (collector.py:284) -- match it here rather
    # than changing a semantic the control loop already depends on.
    solar.save_config(st._db, daily_request_cap=3)

    woke = []

    class _Client:
        async def resolve_vin(self): return "VIN1"
        async def wake_up(self, vin):
            woke.append(vin)
            return {"state": "online"}

    monkeypatch.setattr(car_routes, "DEMO", False)
    monkeypatch.setattr(car_routes, "_client", lambda: _Client())
    monkeypatch.setattr(car_routes, "store", lambda: st)
    monkeypatch.setattr(car_routes.settings, "vin", "VIN1")
    # This test is about the daily cap, not the one-per-minute wake limit;
    # three wakes back to back would otherwise trip the latter first.
    monkeypatch.setattr(car_routes, "WAKE_MIN_INTERVAL_S", 0)

    import asyncio
    # Two wakes fit inside a cap of 3; the third trips it.
    asyncio.run(car_routes.car_wake())
    asyncio.run(car_routes.car_wake())
    assert len(woke) == 2
    assert solar.load_state(st._db, "VIN1")["requests_today"] == 2, (
        "each wake must be counted, not just the collector's own polls")

    # The third must be refused -- and must NOT reach the car.
    from fastapi import HTTPException
    try:
        asyncio.run(car_routes.car_wake())
        raised = None
    except HTTPException as exc:
        raised = exc
    assert raised is not None and raised.status_code == 429, (
        "over the cap, a wake must be refused rather than billed")
    assert len(woke) == 2, "the refused wake must never have touched the car"
    st.close()


async def _fake_vin():
    return "5YJSA00000F000000"


class _FakeClient:
    def __init__(self):
        self.cache = _FakeCache()

    async def command(self, vin, cmd_id, body):
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        return {"state": "online"}


def _fake_client():
    return _FakeClient()


@pytest.mark.asyncio
async def test_a_command_is_counted_against_the_daily_cap(monkeypatch):
    """The cap guarded only the collector. POST /api/car/command/{id} spent
    entirely outside it, so requests_today reported a comfortable number
    while the budget drained through a door it did not watch."""
    spent = []
    monkeypatch.setattr(car_routes, "_spend", lambda vin: spent.append(vin))
    monkeypatch.setattr(car_routes, "DEMO", False)
    monkeypatch.setattr(car_routes, "_vin", _fake_vin)
    monkeypatch.setattr(car_routes, "_client", _fake_client)

    await car_routes.car_command("flash_lights", {})
    assert spent == ["5YJSA00000F000000"], "the command did not book a request"


@pytest.mark.asyncio
async def test_a_second_wake_inside_the_window_is_refused(monkeypatch):
    """A wake is $0.02 -- the most expensive request this system makes --
    against a $10/month credit. Nothing stopped a button from being held
    down."""
    monkeypatch.setattr(car_routes, "_spend", lambda vin: None)
    monkeypatch.setattr(car_routes, "DEMO", False)
    monkeypatch.setattr(car_routes, "_vin", _fake_vin)
    monkeypatch.setattr(car_routes, "_client", _fake_client)
    monkeypatch.setattr(car_routes, "_last_wake_ts", 0.0)

    await car_routes.car_wake()
    with pytest.raises(HTTPException) as exc:
        await car_routes.car_wake()
    assert exc.value.status_code == 429
