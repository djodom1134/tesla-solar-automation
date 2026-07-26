from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import collector
import solar
import tesla
from store import Store

S = SimpleNamespace(poll_driving=120, poll_charging=300, poll_idle=900,
                    poll_asleep=300)


class StopTest(Exception):
    """Raised by a faked asyncio.sleep to break run()'s infinite loop once
    enough ticks have been observed."""


def test_engaged_uses_the_solar_period_over_every_other_rule():
    assert collector.next_interval(
        "online", {"shift": "P", "charging": True}, S, solar_engaged=120) == 120


def test_not_engaged_keeps_the_existing_behaviour():
    assert collector.next_interval(
        "online", {"shift": "P", "charging": True}, S, solar_engaged=0) == 300
    assert collector.next_interval("offline", None, S, solar_engaged=0) == 300


def test_engagement_never_overrides_a_sleeping_car():
    """A sleeping car must not be polled fast; the loop idles instead."""
    assert collector.next_interval("asleep", None, S, solar_engaged=120) == 300


def test_should_refresh_view_predicate():
    """With view_refresh_ticks=5 and no writes, 5 engaged ticks must issue
    exactly ONE vehicle_data and ZERO state checks. This predicate is the
    decision; test_run_engaged_ticks_batch_vehicle_data below proves the loop
    actually uses it."""
    assert collector.should_refresh_view(ticks_since_view=0, refresh_every=5,
                                         wrote_last_tick=False) is False
    assert collector.should_refresh_view(ticks_since_view=5, refresh_every=5,
                                         wrote_last_tick=False) is True
    assert collector.should_refresh_view(ticks_since_view=0, refresh_every=5,
                                         wrote_last_tick=True) is True


# --------------------------------------------------------------------------
# _command's truth table -- the invariant the whole controller rests on.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,expected", [
    (200, {"response": {"result": True}}, True),
    (200, {"response": {"result": False, "reason": "not_charging"}}, False),
    (408, {}, False),
])
async def test_command_reports_success_only_on_a_real_ack(status, body, expected):
    """HTTP 200 with result:false is a REFUSAL. Treating it as success makes
    the controller integrate against an amps value the car never adopted."""
    class FakeClient:
        async def command(self, vin, name, params):
            return status, body

    ok = await collector._command(FakeClient(), "VIN1", "set_charging_amps",
                                  charging_amps=10)
    assert ok is expected


@pytest.mark.asyncio
async def test_command_returns_false_on_a_transport_error_rather_than_propagating():
    """I1: a proxy outage raises httpx's own exception types, not
    TeslaAPIError/TeslaAuthError. Letting one escape _command kills the whole
    daemon on a transient network blip."""
    class FakeClient:
        async def command(self, vin, name, params):
            raise httpx.ConnectError("connection refused")

    ok = await collector._command(FakeClient(), "VIN1", "set_charging_amps",
                                  charging_amps=10)
    assert ok is False


# --------------------------------------------------------------------------
# Loop-level call-pattern proof. The predicate test above cannot catch a
# regression where the loop bypasses should_refresh_view entirely.
# --------------------------------------------------------------------------

class _FakeLoopClient:
    """Records every billable call name. vehicle_data always succeeds so the
    car never appears to fall asleep mid-test."""

    def __init__(self, calls: list[str]):
        self.calls = calls

    async def resolve_vin(self):
        return "VIN1"

    async def energy_sites(self):
        return [{"energy_site_id": 1}]

    async def vehicle(self, vin):
        self.calls.append("state")
        return {"state": "online"}

    async def vehicle_data(self, vin, *a, **k):
        self.calls.append("data")
        return {}

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_run_engaged_ticks_batch_vehicle_data(monkeypatch, tmp_path):
    """Three billable calls per tick is $22/month; this pattern is ~1.4.

    Drives the real run() loop -- not just the predicate -- for 6 engaged
    ticks against a fake client. solar_tick itself is stubbed to hold
    "charging" unconditionally: the solar decision logic is solar.py's job,
    already covered elsewhere; this test is only about the loop's call
    pattern around should_refresh_view."""
    calls: list[str] = []
    db_path = tmp_path / "car.db"

    seed = Store(db_path)
    solar.save_config(seed._db, enabled=1)   # a tmp, throwaway DB -- not the owner's
    seed.close()

    monkeypatch.setattr(collector.settings, "db_file", db_path)
    monkeypatch.setattr(collector, "TeslaClient", lambda settings: _FakeLoopClient(calls))

    async def fake_solar_tick(client, store_, vin, view, cfg, site_id):
        return "charging", False
    monkeypatch.setattr(collector, "solar_tick", fake_solar_tick)

    sleep_count = 0

    async def fake_sleep(seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            calls.clear()   # discard the bootstrap tick's unavoidable state+data pair
        if sleep_count >= 7:
            raise StopTest()
    monkeypatch.setattr(collector.asyncio, "sleep", fake_sleep)

    with pytest.raises(StopTest):
        await collector.run()

    assert calls.count("state") == 0, "an engaged tick must never re-check state"
    assert calls.count("data") == 1, "6 engaged ticks at view_refresh_ticks=5 must issue exactly one read"


# --------------------------------------------------------------------------
# Crash recovery: C1 -- a refused restore must leave dirty set, not erase the
# only record of the owner's real amps and limit.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recovery_leaves_dirty_set_when_the_restore_is_refused(tmp_path, monkeypatch):
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_state(db, "VIN1", dirty=1, original_amps=16, original_limit=90)

    monkeypatch.setattr(tesla, "proxy_up", lambda url: True)

    class FakeClient:
        async def command(self, vin, name, params):
            # The car still holds the solar amps -- this refusal is exactly
            # the scenario a real proxy returns when the car isn't charging.
            return 200, {"response": {"result": False, "reason": "not_charging"}}

    cfg = SimpleNamespace(proxy_url="https://localhost:4443")
    ok = await collector.recover(FakeClient(), db, "VIN1",
                                 solar.load_state(db, "VIN1"), "home", cfg)

    assert ok is False
    st = solar.load_state(db, "VIN1")
    assert st["dirty"] == 1, "a refused restore must not clear dirty"
    assert st["original_amps"] == 16, "the only way back to the owner's amps must survive"
    assert st["original_limit"] == 90
    store_.close()


@pytest.mark.asyncio
async def test_recovery_resets_the_machine_not_just_the_flags(tmp_path, monkeypatch):
    """After recovery the machine must be clean idle.

    _restore() nulls the originals; a machine left in "charging" would resume
    next tick, emit set_amps, and command the car with nothing recorded to
    restore. The re-arm guard only covers charge_start, so it cannot catch it.
    """
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    # seed: dirty, mid-charge, originals recorded -- exactly what a crash
    # mid-session leaves behind.
    solar.save_state(db, "VIN1", dirty=1, original_amps=16, original_limit=90,
                     state="charging", breach_ticks=1, recover_ticks=1,
                     grace_s_elapsed=45, hold_s=30, engaged_at=1234)

    monkeypatch.setattr(tesla, "proxy_up", lambda url: True)

    class FakeClient:
        async def command(self, vin, name, params):
            return 200, {"response": {"result": True}}

    cfg = SimpleNamespace(proxy_url="https://localhost:4443")
    ok = await collector.recover(FakeClient(), db, "VIN1",
                                 solar.load_state(db, "VIN1"), "home", cfg)

    assert ok is True
    st = solar.load_state(db, "VIN1")
    assert st["dirty"] == 0
    assert st["state"] == "idle", "a leftover 'charging' would go straight to set_amps next tick"
    assert st["breach_ticks"] == 0
    assert st["recover_ticks"] == 0
    assert st["grace_s_elapsed"] == 0
    assert st["hold_s"] == 0
    store_.close()
