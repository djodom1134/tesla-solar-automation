from __future__ import annotations

import time

import pytest

import ha_routes
import solar
from store import Store


def test_ha_routes_cannot_spend_a_tesla_request():
    """THE cost guarantee, enforced structurally rather than by intention.

    An HA dashboard on a wall tablet polls every 60 s forever. If any of that
    reached the Fleet API it would drain a $10/month credit quietly. This
    module must therefore be incapable of it -- not merely careful.
    """
    import ast
    import inspect

    # Parsed, not grepped. A text scan trips over its own documentation --
    # this docstring names the very calls it forbids -- and would pass a
    # module that imported the client under an alias. The AST sees what the
    # module actually imports.
    tree = ast.parse(inspect.getsource(ha_routes))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "tesla" not in imported, (
        "ha_routes imports the Tesla client -- HA polls this endpoint every "
        "60 s forever, and every one of those must be free")

    # And nothing it does import may drag the client in transitively.
    import sys
    for mod in imported:
        m = sys.modules.get(mod)
        if m is None:
            continue
        assert not hasattr(m, "TeslaClient"), (
            f"ha_routes imports {mod!r}, which exposes TeslaClient")


def test_collector_running_judges_against_the_cadence_in_force():
    """A fixed threshold is wrong in both directions here: the loop sleeps
    120 s while engaged and 1800 s after dark, so one constant either misses
    a dead collector all day or reports a healthy one dead every night."""
    now = 1_000_000.0

    # Engaged: a 120 s cadence, beat 200 s ago -- alive.
    assert ha_routes.collector_running(
        {"heartbeat_ts": now - 200, "heartbeat_sleep_s": 120}, now)
    # Same age, but silent for 20 minutes at that cadence -- dead.
    assert not ha_routes.collector_running(
        {"heartbeat_ts": now - 1200, "heartbeat_sleep_s": 120}, now)
    # After dark on a 1800 s cadence, 1200 s of silence is NORMAL.
    assert ha_routes.collector_running(
        {"heartbeat_ts": now - 1200, "heartbeat_sleep_s": 1800}, now)


def test_no_heartbeat_means_not_running_never_assumed_alive():
    """Unknown is not alive. A stale number dressed as current is worse than
    no number -- the doctrine this whole project is built on."""
    now = 1_000_000.0
    assert not ha_routes.collector_running({"heartbeat_ts": None}, now)
    assert not ha_routes.collector_running({}, now)


@pytest.mark.asyncio
async def test_state_reports_schema_and_the_three_clocks(tmp_path, monkeypatch):
    """schema exists so HA can tell data from an error body: the rest platform
    never calls raise_for_status(), so a 401 arrives as a parseable dict."""
    st = Store(tmp_path / "car.db")
    solar.save_config(st._db, enabled=1)
    solar.save_state(st._db, "VIN1", heartbeat_ts=int(time.time()),
                     heartbeat_sleep_s=120)
    st.record({"vin": "VIN1", "soc": 55, "limit": 80, "sampled_at": int(time.time()),
               "charging_state": "Charging", "amps_actual": 12, "charging": 1,
               "lat": 40.0, "lon": -105.0}, at_home=True)
    monkeypatch.setattr(ha_routes, "DEMO", False)
    monkeypatch.setattr(ha_routes, "store", lambda: st)
    monkeypatch.setattr(ha_routes.settings, "vin", "VIN1")

    body = await ha_routes.ha_state()

    assert body["schema"] == 1
    assert body["collector_running"] is True
    assert body["soc"] == 55
    assert body["plugged_in"] is True and body["charging"] is True
    assert "heartbeat_age_s" in body and "snapshot_age_s" in body
    st.close()


@pytest.mark.asyncio
async def test_meters_return_null_not_zero_when_the_car_is_unknown(tmp_path, monkeypatch):
    """0 would read to HA as a counter reset on a total_increasing sensor,
    and its reset rule sets the zero-point to 0 -- injecting the whole
    lifetime total into one 5-minute bucket, with the cost sensor booking
    money to match. null becomes `unavailable`, which HA discards safely.
    """
    st = Store(tmp_path / "car.db")
    monkeypatch.setattr(ha_routes, "DEMO", False)
    monkeypatch.setattr(ha_routes, "store", lambda: st)
    monkeypatch.setattr(ha_routes.settings, "vin", "")

    body = await ha_routes.ha_meters()
    assert body["car_solar_kwh"] is None, "null, never 0"
    assert body["car_total_kwh"] is None
    st.close()
