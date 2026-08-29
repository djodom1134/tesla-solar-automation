from __future__ import annotations

import os

os.environ["DEMO"] = "1"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import time

import garage
import solar
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


def test_status_reports_zero_free_miles_before_anything_runs(client, monkeypatch):
    """Task 20's honest today: the controller has never charged from
    surplus, so lifetime free miles must be a real, definite zero -- and
    the share must be None (not 0), since 0 of 0 tracked miles is
    undefined, not a real 0%."""
    monkeypatch.setattr(solar_routes, "DEMO", False)
    body = client.get("/api/car/solar/status").json()
    assert body["free_miles_driven"] == 0
    assert body["tracked_miles"] == 0
    assert body["free_miles_share"] is None
    assert body["free_miles_since"] is None


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


def test_demo_status_pins_the_lifetime_free_miles_fixture(client):
    """Task 20's promoted headline: 128.4 of 431.7 miles is the brief's own
    worked example (29.7%), pinned here the same way the rest of the demo
    fixture is pinned above."""
    body = client.get("/api/car/solar/status").json()
    assert body["free_miles_driven"] == 128.4
    assert body["tracked_miles"] == 431.7
    assert body["free_miles_share"] == 29.7


def test_status_free_miles_share_divides_the_lifetime_totals(client, monkeypatch, tmp_path):
    """Not just passed through -- computed from whatever the ledger has
    actually accumulated, real store-backed path.

    Written through a SEPARATE Store connection onto the same on-disk file
    (WAL mode), mirroring how the real collector process and this web app
    process share car.db as two different connections -- sqlite3
    connections are thread-bound (check_same_thread), so writing through
    solar_routes' own connection directly from the test thread and then
    reading it back through TestClient's portal thread raises
    "SQLite objects created in a thread can only be used in that same
    thread"; two independent connections to the same file have no such
    restriction and is exactly how the real system works anyway.

    _vin() prefers settings.vin (empty in this test environment, see
    test_status_reports_idle_before_anything_runs's neighbours) and falls
    back to the most recent samples.vin -- so a sample row is inserted
    first, purely to give the route a vin to key solar_state on.
    """
    monkeypatch.setattr(solar_routes, "DEMO", False)
    from store import Store
    import solar as solar_module
    writer = Store(tmp_path / "t.db")
    writer._db.execute("INSERT INTO samples (ts, vin) VALUES (1000, 'VINX')")
    writer._db.commit()
    solar_module.save_state(writer._db, "VINX", free_miles_driven=25.0,
                            tracked_miles=100.0, free_miles_since=1785000000)
    writer.close()

    body = client.get("/api/car/solar/status").json()
    assert body["free_miles_driven"] == 25.0
    assert body["tracked_miles"] == 100.0
    assert body["free_miles_share"] == 25.0
    assert body["free_miles_since"] == 1785000000


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


def test_tariff_round_trips_as_a_real_number_not_a_floored_integer(client):
    """The tariff is cents per kWh, so int() coercion would floor $0.12 to $0
    and every money figure in the app would silently read zero.

    The owner's actual rates: $0.12/kWh imported, $0.04/kWh credited for
    export. The 3:1 spread is the whole economic case for self-consumption --
    at full-retail net metering it would be zero.
    """
    r = client.put("/api/car/solar/config",
                   json={"import_rate": 0.12, "export_rate": 0.04})
    assert r.status_code == 200, r.text

    cfg = client.get("/api/car/solar/config").json()
    assert cfg["import_rate"] == pytest.approx(0.12), (
        f"got {cfg['import_rate']!r} -- int() coercion floors a cent rate to zero")
    assert cfg["export_rate"] == pytest.approx(0.04)


def test_tariff_can_be_cleared_back_to_unconfigured(client):
    """NULL means "not configured" and must suppress money figures rather
    than showing a confident $0.00."""
    client.put("/api/car/solar/config", json={"import_rate": 0.12})
    r = client.put("/api/car/solar/config", json={"import_rate": None})
    assert r.status_code == 200, r.text
    assert client.get("/api/car/solar/config").json()["import_rate"] is None


def test_an_implausible_tariff_is_rejected(client):
    """A fat-fingered 12 (dollars) instead of 0.12 must not be accepted and
    then reported as a hundred-fold cost."""
    r = client.put("/api/car/solar/config", json={"import_rate": 12})
    assert r.status_code == 400
    assert "import_rate" in r.text


def _saved_config():
    """Read solar_config back on a connection of our OWN.

    TestClient dispatches routes on a worker thread, so the Store the route
    created belongs to that thread and sqlite3 refuses to hand it back here.
    Reopening the file is also the stricter check: it proves the value was
    committed, not merely held in the writer's transaction.
    """
    import sqlite3

    from config import settings
    db = sqlite3.connect(settings.db_file)
    db.row_factory = sqlite3.Row
    try:
        return solar.load_config(db)
    finally:
        db.close()


def test_charge_mode_starts_at_off(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    body = client.get("/api/car/charge-mode").json()
    assert body["mode"] == "off"
    assert body["expires_ts"] is None


def test_setting_now_stamps_the_next_midnight(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    body = client.put("/api/car/charge-mode", json={"mode": "now"}).json()
    assert body["mode"] == "now"

    from config import settings
    assert body["expires_ts"] == solar.next_midnight_ts(
        settings.timezone, time.time())
    assert _saved_config()["force_charge_until"] == body["expires_ts"]


def test_setting_solar_clears_the_force_and_enables(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.put("/api/car/charge-mode", json={"mode": "now"})
    body = client.put("/api/car/charge-mode", json={"mode": "solar"}).json()

    assert body["mode"] == "solar"
    assert body["expires_ts"] is None
    conf = _saved_config()
    assert conf["force_charge_until"] is None
    assert conf["enabled"] == 1


def test_setting_off_clears_the_force_and_disables(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.put("/api/car/charge-mode", json={"mode": "now"})
    body = client.put("/api/car/charge-mode", json={"mode": "off"}).json()

    assert body["mode"] == "off"
    conf = _saved_config()
    assert conf["force_charge_until"] is None
    assert conf["enabled"] == 0


def test_an_unknown_mode_is_refused(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    assert client.put(
        "/api/car/charge-mode", json={"mode": "fast"}).status_code == 400
    assert client.put("/api/car/charge-mode", json={}).status_code == 400


def test_force_charge_until_is_not_writable_through_the_config_route(
        client, monkeypatch):
    """A client that could set the timestamp directly could set it a year
    out, and the midnight expiry -- the whole safety property of "now" --
    would be gone. The mode route is the only way in."""
    monkeypatch.setattr(solar_routes, "DEMO", False)
    r = client.put("/api/car/solar/config",
                   json={"force_charge_until": 99999999999})
    assert r.status_code == 400
    assert "unknown fields" in r.json()["detail"]


# --- the manual-override pause --------------------------------------------

def _seeded_db():
    """A connection of OUR OWN to the routes' database.

    Never solar_routes.store()._db: TestClient runs the request in a
    different thread, and a store first created from the test thread makes
    every subsequent route call fail on sqlite's thread check. Same shape as
    _saved_config above, for the same reason.
    """
    import sqlite3
    from config import settings
    db = sqlite3.connect(settings.db_file)
    db.row_factory = sqlite3.Row
    return db


def _pause(client, monkeypatch, amps=32):
    """Latch a pause on the VIN the routes will look at."""
    from config import settings
    monkeypatch.setattr(settings, "vin", "VIN1")
    client.get("/api/car/charge-mode")     # creates the schema, in ITS thread
    db = _seeded_db()
    try:
        solar.save_config(db, enabled=1)
        solar.save_state(db, "VIN1", override_amps=amps, override_since=1000,
                         commanded_amps=24, commanded_ack=1)
    finally:
        db.close()


def _loaded_state():
    db = _seeded_db()
    try:
        return solar.load_state(db, "VIN1")
    finally:
        db.close()


def test_an_override_reports_the_manual_mode(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    _pause(client, monkeypatch)
    body = client.get("/api/car/charge-mode").json()
    assert body["mode"] == "manual"
    # `enabled` deliberately stays true: the owner has not switched the
    # feature off, it is standing aside. The HA switch and the setup
    # checkbox both read this, and both would be wrong if it flipped.
    assert body["enabled"] is True


def test_the_status_card_gets_the_owners_own_rate(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    _pause(client, monkeypatch, amps=32)
    body = client.get("/api/car/solar/status").json()
    assert body["mode"] == "manual"
    assert body["override_amps"] == 32
    assert body["override_since"] == 1000


def test_choosing_solar_from_the_car_page_clears_the_pause(client, monkeypatch):
    """The second half of the release condition: re-enabling automated
    control from the UI, without waiting for a stop and a start."""
    monkeypatch.setattr(solar_routes, "DEMO", False)
    _pause(client, monkeypatch)

    body = client.put("/api/car/charge-mode", json={"mode": "solar"}).json()

    assert body["mode"] == "solar"
    st = _loaded_state()
    assert st["override_amps"] is None
    assert st["commanded_amps"] is None, (
        "a stale command would let the next tick latch all over again")


def test_forcing_from_the_car_page_also_clears_the_pause(client, monkeypatch):
    """Not limited to mode=solar: choosing "now" settles who is driving just
    as squarely, and a latch left behind would flip the mode back to manual
    the moment the force expired at midnight."""
    monkeypatch.setattr(solar_routes, "DEMO", False)
    _pause(client, monkeypatch)

    client.put("/api/car/charge-mode", json={"mode": "now"})

    assert _loaded_state()["override_amps"] is None


def test_the_pause_setting_round_trips(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.get("/api/car/solar/config")     # creates the schema, in ITS thread
    assert _saved_config()["pause_on_override"] == 1     # on by default
    r = client.put("/api/car/solar/config", json={"pause_on_override": 0})
    assert r.status_code == 200
    assert _saved_config()["pause_on_override"] == 0


def test_switching_solar_back_on_releases_the_pause(client, monkeypatch):
    """An owner who paused, turned the feature off, then turned it back on
    would otherwise land straight back in "manual" with the controller still
    standing aside and nothing on screen explaining why."""
    monkeypatch.setattr(solar_routes, "DEMO", False)
    _pause(client, monkeypatch)
    client.put("/api/car/solar/config", json={"enabled": 0})

    client.put("/api/car/solar/config", json={"enabled": 1})

    assert _loaded_state()["override_amps"] is None


def test_an_unrelated_save_does_not_cancel_the_pause(client, monkeypatch):
    """The setup page posts the whole form on every save, `enabled` included.
    Only a real off -> on transition means "resume"; adjusting the ramp does
    not, and silently clearing the pause there would hand the car back
    without the owner ever asking."""
    monkeypatch.setattr(solar_routes, "DEMO", False)
    _pause(client, monkeypatch)

    r = client.put("/api/car/solar/config", json={"enabled": 1, "ramp_a": 6})

    assert r.status_code == 200
    assert _loaded_state()["override_amps"] == 32
