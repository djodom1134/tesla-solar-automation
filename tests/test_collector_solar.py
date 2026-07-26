from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import collector
import home
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


# --------------------------------------------------------------------------
# C5 -- recover() must run once at process startup, never once per tick.
# --------------------------------------------------------------------------

class _SteadyExportClient:
    """6 kW of pure solar export, unchanging tick to tick -- the healthy,
    already-engaged state the loop should hold indefinitely."""

    def __init__(self):
        self.commands = []

    async def _get(self, path, ttl=0):
        return {"grid_power": -6000.0, "solar_power": 6000.0}

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        raise AssertionError("must never wake a car that never slept")


@pytest.mark.asyncio
async def test_c5_steady_export_holds_a_single_engagement_across_six_ticks(
    tmp_path, monkeypatch,
):
    """C5 regression -- drives the REAL collector.solar_tick, not a stub.

    `dirty=1` is the NORMAL, HEALTHY condition for the whole duration of an
    engagement: it is set on charge_start and cleared only on restore. A
    recover() call from INSIDE solar_tick (rather than once at process
    startup) sees dirty=1 on the tick right after an ordinary charge_start
    and restores the owner's amps/limit, resetting the machine to idle --
    every other tick. Because that reset happens before `charging -> grace
    -> stopped` (the only path that issues charge_stop) can ever run its
    course, a steady, healthy 6 kW export cycles charge_start/restore
    forever and charge_stop is never reached -- starting a charge the owner
    never asked for and importing from the grid all night.

    Six ticks of steady export -- the ordinary, healthy case -- must settle
    into ONE engagement.
    """
    monkeypatch.setattr(tesla, "proxy_up", lambda url: True)  # deterministic,
    # in case the per-tick recover() call is ever reinstated (see this test's
    # discrimination check in the fix-wave report).

    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)      # a tmp, throwaway DB -- not the owner's
    home.save(db, 40.0, -105.0, 100)
    # Pre-seed hold_s past start_hold_s so engagement fires on the very first
    # tick -- all six ticks then exercise the STEADY, already-engaged case the
    # bug report describes, rather than spending ticks on the initial dwell.
    solar.save_state(db, "VIN1", state="idle", hold_s=10_000)

    client = _SteadyExportClient()
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    view = {
        "charging_state": "Charging", "amps_actual": 24, "amps_max": 48,
        "volts": 240, "charge_amps": 24, "soc": 55, "limit": 80,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }

    states = []
    for _ in range(6):
        state, _ = await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)
        states.append(state)

    starts = [c for c in client.commands if c[0] == "charge_start"]
    limits = [c for c in client.commands if c[0] == "set_charge_limit"]

    assert len(starts) == 1, (
        f"charge_start issued {len(starts)} times across states={states}: "
        f"{client.commands}")
    assert len(limits) == 0, f"set_charge_limit issued {len(limits)} times: {client.commands}"
    assert all(s == "charging" for s in states), f"states were {states}"
    assert solar.load_state(db, "VIN1")["state"] == "charging"
    store_.close()


# --------------------------------------------------------------------------
# I7 -- a failed wake must not retry on the immediately following tick.
# --------------------------------------------------------------------------

class _WakeFailsClient:
    """Steady export sufficient to restart from `stopped`, but wake_up always
    fails -- a flaky proxy or a Tesla-side wake timeout."""

    def __init__(self):
        self.wake_calls = 0
        self.commands = []

    async def _get(self, path, ttl=0):
        return {"grid_power": -3000.0, "solar_power": 3000.0}

    async def wake_up(self, vin):
        self.wake_calls += 1
        raise tesla.TeslaAPIError(408, "offline")

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        return 200, {"response": {"result": True}}


@pytest.mark.asyncio
async def test_wake_failure_resets_the_hold_so_the_next_tick_does_not_retry(tmp_path):
    """I7: without persisting a reset hold, the machine stays 'stopped' with
    hold_s already at restart_hold_s, and advance() re-emits ["wake", ...] on
    the very next tick -- at $0.02/wake and a 120s period, ~$0.60/hour until
    the daily cap intervenes ~13h later."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="stopped", hold_s=300)  # >= restart_hold_s

    client = _WakeFailsClient()
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    view = {
        "charging_state": "Stopped", "amps_actual": None, "amps_max": 48,
        "volts": None, "charge_amps": 5, "soc": 50, "limit": 80,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }

    state, wrote = await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)
    assert state == "stopped"
    assert wrote is False
    st = solar.load_state(db, "VIN1")
    assert st["state"] == "stopped"
    assert st["hold_s"] == 0, "a stale restart_hold_s-sized hold would retry the wake next tick"

    # Second tick, same conditions: the dwell must re-accumulate, not retry.
    state, _ = await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)
    assert state == "stopped"
    assert client.wake_calls == 1, "a failed wake must not retry on the immediately following tick"
    store_.close()


# --------------------------------------------------------------------------
# I8 -- spec 3.2's `target != last_acknowledged_a` guard.
# --------------------------------------------------------------------------

class _CommandRecordingClient:
    def __init__(self, grid_w):
        self.grid_w = grid_w
        self.commands = []

    async def _get(self, path, ttl=0):
        return {"grid_power": self.grid_w, "solar_power": abs(self.grid_w)}

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        raise AssertionError("must not wake while already charging")


@pytest.mark.asyncio
async def test_set_amps_is_not_resent_when_the_car_already_holds_the_target(tmp_path):
    """I8: reproduces the reviewer's EVSE-pilot-limit scenario -- amps_actual
    held at 40A by the EVSE while amps_max reads 48A. The law pins target at
    the 48A clamp every tick because the error never enters the deadband, but
    the car already holds 48A as its acknowledged charge_current_request.
    Without the guard, solar_tick re-sends set_charging_amps(48) every tick
    forever."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="charging", dirty=1, original_amps=5,
                     engaged_at=1)

    client = _CommandRecordingClient(grid_w=-5000.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    view = {
        "charging_state": "Charging", "amps_actual": 40, "amps_max": 48,
        "volts": 240, "charge_amps": 48, "soc": 60, "limit": 80,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }

    for _ in range(3):
        state, _ = await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)
        assert state == "charging"

    amps_commands = [c for c in client.commands if c[0] == "set_charging_amps"]
    assert amps_commands == [], f"re-sent an already-acknowledged value: {amps_commands}"
    store_.close()


@pytest.mark.asyncio
async def test_set_amps_still_writes_unconditionally_on_grace_entry(tmp_path):
    """Grace entry is the one permitted ramp violation (spec 3.2/3.3) and
    must always write min_a, even in the (contrived) case where the car
    already happens to hold that exact value."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    # One breach tick already recorded; this tick's second consecutive
    # breach must fire the grace transition.
    solar.save_state(db, "VIN1", state="charging", breach_ticks=1, dirty=1,
                     original_amps=5, engaged_at=1)

    # A tiny surplus below the floor forces raw_target < min_a (5).
    client = _CommandRecordingClient(grid_w=400.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    view = {
        "charging_state": "Charging", "amps_actual": 5, "amps_max": 48,
        "volts": 240,
        "charge_amps": 5,      # already holds min_a -- must not suppress the write
        "soc": 60, "limit": 80,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }

    state, wrote = await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)
    assert state == "grace"
    assert wrote is True
    amps_commands = [c for c in client.commands if c[0] == "set_charging_amps"]
    assert amps_commands == [("set_charging_amps", {"charging_amps": 5})]
    store_.close()


# --------------------------------------------------------------------------
# I9 -- _restore() must not unconditionally rewrite the charge limit.
# --------------------------------------------------------------------------

class _LimitRecordingClient:
    def __init__(self):
        self.calls = []

    async def command(self, vin, name, params):
        self.calls.append((name, dict(params)))
        return 200, {"response": {"result": True}}


@pytest.mark.asyncio
async def test_restore_reverts_the_limit_only_when_the_car_still_holds_the_raise(tmp_path):
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_state(db, "VIN1", original_amps=10, original_limit=80, raised_to=90)

    client = _LimitRecordingClient()
    st = solar.load_state(db, "VIN1")
    ok, commanded = await collector._restore(client, db, "VIN1", st, {"limit": 90})

    assert ok is True and commanded is True
    assert ("set_charge_limit", {"percent": 80}) in client.calls
    store_.close()


@pytest.mark.asyncio
async def test_restore_skips_the_limit_when_the_owner_changed_it_mid_session(tmp_path):
    """I9: an unconditional set_charge_limit on every restore would silently
    revert an owner who changed the limit in the Tesla app while the
    controller was raised."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_state(db, "VIN1", original_amps=10, original_limit=80, raised_to=90)

    client = _LimitRecordingClient()
    st = solar.load_state(db, "VIN1")
    # The owner set the limit to 85% in the app; the car no longer holds the
    # 90% the controller raised it to.
    ok, commanded = await collector._restore(client, db, "VIN1", st, {"limit": 85})

    assert ok is True
    assert not any(name == "set_charge_limit" for name, _ in client.calls)
    store_.close()


@pytest.mark.asyncio
async def test_restore_skips_the_limit_when_it_was_never_raised(tmp_path):
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_state(db, "VIN1", original_amps=10, original_limit=80, raised_to=None)

    client = _LimitRecordingClient()
    st = solar.load_state(db, "VIN1")
    ok, commanded = await collector._restore(client, db, "VIN1", st, {"limit": 80})

    assert ok is True
    assert not any(name == "set_charge_limit" for name, _ in client.calls)
    store_.close()


@pytest.mark.asyncio
async def test_restore_with_no_view_skips_the_limit_and_logs(tmp_path, capsys):
    """A caller with genuinely no view available must skip the limit restore
    rather than guess -- and must say so rather than silently doing
    nothing."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_state(db, "VIN1", original_amps=10, original_limit=80, raised_to=90)

    client = _LimitRecordingClient()
    st = solar.load_state(db, "VIN1")
    ok, commanded = await collector._restore(client, db, "VIN1", st, None)

    assert ok is True
    assert not any(name == "set_charge_limit" for name, _ in client.calls)
    assert "view" in capsys.readouterr().out.lower()
    store_.close()
