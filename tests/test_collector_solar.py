from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import httpx
import pytest

import collector
import green
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
    # charging_state is "Stopped", not "Charging": this test is specifically
    # about the START transition (idle -> charging via a sustained-surplus
    # hold), which the C5 regression is about. Task 21 adds a SECOND,
    # distinct way into "charging" -- ADOPT, for a car already drawing power
    # on its own -- and a "Charging" car here would now be adopted instead of
    # started, which is a different transition with its own coverage
    # (test_collector_solar.py's adoption-journey tests). Keeping this one
    # "Stopped" preserves the original, documented intent of this test.
    view = {
        "charging_state": "Stopped", "amps_actual": 24, "amps_max": 48,
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
    the daily cap intervenes ~13h later.

    Driven through the SLEEPING path (no live view, snapshot only), because
    that is now the only way a wake is issued at all: an online car skips it,
    since waking a car we just read live costs $0.02 for nothing.
    """
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="stopped", hold_s=300)  # >= restart_hold_s

    client = _WakeFailsClient()
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    store_.record({"charging_state": "Stopped", "amps_actual": 0, "charging": 0,
                   "amps_max": 48, "volts": 240, "charge_amps": 5,
                   "soc": 50, "limit": 80, "lat": 40.0, "lon": -105.0,
                   "fast_charger_present": False, "fast_charger": None,
                   "vin": "VIN1", "sampled_at": int(time.time())}, at_home=True)
    view = None

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


# --------------------------------------------------------------------------
# Task 14 -- the dynamic charge-limit raise (spec 3.4).
# --------------------------------------------------------------------------

class _NearLimitSteadyExportClient:
    """Steady 3 kW export with the car already charging 1% under its limit --
    exactly the scenario that should trigger one raise and then hold it."""

    def __init__(self):
        self.commands: list[tuple[str, dict]] = []

    async def _get(self, path, ttl=0):
        return {"grid_power": -3000.0, "solar_power": 3000.0}

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        raise AssertionError("must never wake an already-charging car")


@pytest.mark.asyncio
async def test_raises_the_charge_limit_exactly_once_across_many_ticks(tmp_path):
    """Task 14: near the limit with sustained export, set_charge_limit must
    fire exactly once across many ticks. raised_to is the guard -- without it
    the hold timer itself does not fall back below raise_hold_s once a raise
    has fired (see raise_decision's fire branch, which returns hold_elapsed_s
    unchanged), so the same command would be re-issued every following tick.
    """
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1, raise_limit=1, raise_hold_s=600,
                      period_s=120, soc_ceiling=90)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="charging", dirty=1, original_amps=24,
                     original_limit=80, engaged_at=1)

    client = _NearLimitSteadyExportClient()
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    view = {
        "charging_state": "Charging", "amps_actual": 24, "amps_max": 48,
        "volts": 240, "charge_amps": 24, "soc": 79, "limit": 80,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }

    for _ in range(8):
        state, _ = await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)
        assert state == "charging"

    limit_commands = [c for c in client.commands if c[0] == "set_charge_limit"]
    assert limit_commands == [("set_charge_limit", {"percent": 90})], (
        f"set_charge_limit issued {len(limit_commands)} times across 8 ticks: "
        f"{client.commands}")
    assert solar.load_state(db, "VIN1")["raised_to"] == 90
    store_.close()


# --------------------------------------------------------------------------
# run()'s startup recovery latch (collector.py:359, 412-429). recover() must
# run ONCE per process, before the very first engaged tick -- never gate
# solar_tick on every subsequent tick. The existing loop test
# (test_run_engaged_ticks_batch_vehicle_data) starts with dirty=0, so
# recover() returns True immediately and the recovery-pending branch
# (collector.py:417-429, the `continue`) is never exercised by anything.
# Deleting the entire `if not recovery_done:` block would leave all other
# tests green while silently removing startup crash recovery.
# --------------------------------------------------------------------------

class _AwayDirtyClient:
    """Reachable and dirty, but parked far from home -- a recovery gate that
    can never pass. wake_up and command must never be reached while
    recovery is pending; solar_tick must never run at all."""

    def __init__(self):
        self.commands: list[tuple[str, dict]] = []

    async def resolve_vin(self):
        return "VIN1"

    async def energy_sites(self):
        return [{"energy_site_id": 1}]

    async def vehicle(self, vin):
        return {"state": "online"}

    async def vehicle_data(self, vin, *a, **k):
        return {
            "charge_state": {"charging_state": "Charging",
                              "charge_current_request": 5,
                              "charger_actual_current": 5,
                              "conn_charge_cable": "IEC"},
            "drive_state": {"latitude": 0.0, "longitude": 0.0},   # far from home
            "vehicle_state": {},
        }

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        raise AssertionError("must not wake while recovery is pending")

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_recovery_pending_blocks_solar_tick(tmp_path, monkeypatch):
    """dirty=1 with a gate that can never pass (car parked away from home):
    solar_tick must never run and no command may ever be issued, tick after
    tick.

    Drives the REAL run(), bounded via a faked asyncio.sleep -- NOT
    once=True. once=True returns from INSIDE the recovery-pending branch
    before the loop ever reaches the `continue` this test guards, so it
    cannot discriminate a missing `continue` (see the report for the
    red/green proof: removing `continue` only fails under the bounded-tick
    style used here).
    """
    monkeypatch.setattr(tesla, "proxy_up", lambda url: True)  # isolate the
    # failure to location, not the proxy gate

    db_path = tmp_path / "car.db"
    seed = Store(db_path)
    home.save(seed._db, 40.0, -105.0, 100)   # home is far from the client's (0, 0)
    solar.save_state(seed._db, "VIN1", dirty=1, original_amps=10, original_limit=80)
    seed.close()

    monkeypatch.setattr(collector.settings, "db_file", db_path)
    client = _AwayDirtyClient()
    monkeypatch.setattr(collector, "TeslaClient", lambda settings: client)

    async def stub_solar_tick(*a, **k):
        raise AssertionError("solar_tick must not run while recovery is pending")
    monkeypatch.setattr(collector, "solar_tick", stub_solar_tick)

    sleep_count = 0

    async def fake_sleep(seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 3:
            raise StopTest()
    monkeypatch.setattr(collector.asyncio, "sleep", fake_sleep)

    with pytest.raises(StopTest):
        await collector.run()

    assert client.commands == [], (
        f"a blocked recovery gate must never command: {client.commands}")


class _HomeChargingClient:
    """Reachable, at home, plugged in, steady solar export -- the ordinary,
    healthy dirty=0 startup that must engage and hold. Used to prove
    recover() runs exactly once, not once per tick."""

    def __init__(self):
        self.commands: list[tuple[str, dict]] = []

    async def resolve_vin(self):
        return "VIN1"

    async def energy_sites(self):
        return [{"energy_site_id": 1}]

    async def vehicle(self, vin):
        return {"state": "online"}

    async def vehicle_data(self, vin, *a, **k):
        return {
            "charge_state": {"charging_state": "Charging",
                              "charger_actual_current": 24,
                              "charge_current_request_max": 48,
                              "charger_voltage": 240,
                              "charge_current_request": 24,
                              "battery_level": 55, "charge_limit_soc": 80,
                              "conn_charge_cable": "IEC",
                              "fast_charger_present": False,
                              "fast_charger_type": None},
            "drive_state": {"latitude": 40.0, "longitude": -105.0},
            "vehicle_state": {},
        }

    async def _get(self, path, ttl=0):
        return {"grid_power": -6000.0, "solar_power": 6000.0}

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        raise AssertionError("must never wake a car that never slept")

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_recovery_latches_once_when_dirty_is_clear(tmp_path, monkeypatch):
    """dirty=0 (the ordinary, healthy start): recover() must run on tick one
    and never again, however many ticks follow.

    recover() itself is harmless to call every tick when dirty=0 -- it
    short-circuits True immediately -- so a behavioural assertion alone
    (state reaches "charging") would stay green even if the `if not
    recovery_done:` gate were deleted entirely. The call-count assertion is
    what actually pins the latch.
    """
    db_path = tmp_path / "car.db"
    seed = Store(db_path)
    solar.save_config(seed._db, enabled=1)     # a tmp, throwaway DB
    home.save(seed._db, 40.0, -105.0, 100)
    # Pre-seed past start_hold_s so engagement fires on the very first tick,
    # matching the C5 test's style above.
    solar.save_state(seed._db, "VIN1", state="idle", hold_s=10_000)
    seed.close()

    monkeypatch.setattr(collector.settings, "db_file", db_path)
    client = _HomeChargingClient()
    monkeypatch.setattr(collector, "TeslaClient", lambda settings: client)

    recover_calls = []
    real_recover = collector.recover

    async def counting_recover(*args, **kwargs):
        recover_calls.append(1)
        return await real_recover(*args, **kwargs)
    monkeypatch.setattr(collector, "recover", counting_recover)

    sleep_count = 0

    async def fake_sleep(seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 4:
            raise StopTest()
    monkeypatch.setattr(collector.asyncio, "sleep", fake_sleep)

    with pytest.raises(StopTest):
        await collector.run()

    assert len(recover_calls) == 1, (
        f"recover() must latch after the first tick, called "
        f"{len(recover_calls)} times")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    st = solar.load_state(conn, "VIN1")
    conn.close()
    assert st["state"] == "charging", "the latch must not have blocked normal engagement"


# --------------------------------------------------------------------------
# Invariant 4 (spec 3.7) -- honour 429/Retry-After by lengthening the next
# interval rather than retrying at the normal cadence. Drives the REAL
# run() loop, not just solar_tick in isolation, because the sleep duration
# is something run() computes AFTER solar_tick returns.
# --------------------------------------------------------------------------

class _RateLimitedThenHealthyClient:
    """live_status 429s for the first `fail_ticks` calls, then clears to a
    steady 6 kW export -- the ordinary already-engaged fixture used
    throughout this file, reused here as the "healthy" side of the event."""

    def __init__(self, fail_ticks=2, retry_after=None):
        self.fail_ticks = fail_ticks
        self.retry_after = retry_after
        self.live_status_calls = 0
        self.commands: list[tuple[str, dict]] = []

    async def resolve_vin(self):
        return "VIN1"

    async def energy_sites(self):
        return [{"energy_site_id": 1}]

    async def vehicle(self, vin):
        return {"state": "online"}

    async def vehicle_data(self, vin, *a, **k):
        return {
            "charge_state": {"charging_state": "Charging",
                              "charger_actual_current": 24,
                              "charge_current_request_max": 48,
                              "charger_voltage": 240,
                              "charge_current_request": 24,
                              "battery_level": 55, "charge_limit_soc": 80,
                              "conn_charge_cable": "IEC",
                              "fast_charger_present": False,
                              "fast_charger_type": None},
            "drive_state": {"latitude": 40.0, "longitude": -105.0},
            "vehicle_state": {},
        }

    async def _get(self, path, ttl=0):
        self.live_status_calls += 1
        if self.live_status_calls <= self.fail_ticks:
            raise tesla.TeslaAPIError(429, "rate limited", retry_after=self.retry_after)
        return {"grid_power": -6000.0, "solar_power": 6000.0}

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        raise AssertionError("must never wake an already-charging car")

    async def aclose(self):
        pass


async def _drive_run_capturing_sleeps(monkeypatch, tmp_path, client, ticks):
    """Shared harness: seeds an already-charging, clean (dirty=0) machine so
    recover() latches immediately without issuing a restore -- same pattern
    as test_recovery_latches_once_when_dirty_is_clear above -- then drives
    the real run() loop for `ticks` iterations, returning the captured sleep
    durations in order plus the final persisted solar_state row."""
    db_path = tmp_path / "car.db"
    seed = Store(db_path)
    solar.save_config(seed._db, enabled=1, period_s=120)
    home.save(seed._db, 40.0, -105.0, 100)
    solar.save_state(seed._db, "VIN1", state="charging")
    seed.close()

    monkeypatch.setattr(collector.settings, "db_file", db_path)
    monkeypatch.setattr(collector, "TeslaClient", lambda settings: client)

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= ticks:
            raise StopTest()
    monkeypatch.setattr(collector.asyncio, "sleep", fake_sleep)

    with pytest.raises(StopTest):
        await collector.run()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    final_state = solar.load_state(conn, "VIN1")
    conn.close()
    return sleeps, final_state


@pytest.mark.asyncio
async def test_429_lengthens_the_interval_and_resets_after_success(tmp_path, monkeypatch):
    """Two consecutive 429s on live_status, then it clears. The interval
    must lengthen exponentially from period_s (120) while they persist --
    240, then 480 -- and drop back to period_s, with the counter cleared,
    the tick after it recovers.

    This is the discriminating test: with collector.py's backoff override
    removed, all four sleeps stay at 120 because solar_tick's early return
    on a caught 429 preserves state="charging", which next_interval alone
    always maps to period_s regardless of how many 429s just happened. See
    the fix-wave report for the verbatim red/green run.
    """
    client = _RateLimitedThenHealthyClient(fail_ticks=2, retry_after=None)
    sleeps, final = await _drive_run_capturing_sleeps(monkeypatch, tmp_path, client, ticks=4)

    assert sleeps[0] == 240, f"first 429 (count=1) must double from period_s: {sleeps}"
    assert sleeps[1] == 480, f"second consecutive 429 (count=2) must double again: {sleeps}"
    assert sleeps[2] == 120, f"the tick that recovers must fall back to period_s: {sleeps}"
    assert sleeps[3] == 120, f"and stay there: {sleeps}"

    assert final["consecutive_429s"] == 0, "a successful request must reset the counter"
    assert final["backoff_s"] == 0
    assert all(name != "charge_start" for name, _ in client.commands), (
        "must not have re-started a charge that was already running")


@pytest.mark.asyncio
async def test_429_honours_a_supplied_retry_after_over_the_exponential_fallback(tmp_path, monkeypatch):
    """A server-supplied Retry-After (45s) must win over what the exponential
    formula alone would ask for (240s) -- the server knows better than any
    heuristic."""
    client = _RateLimitedThenHealthyClient(fail_ticks=1, retry_after=45)
    sleeps, _ = await _drive_run_capturing_sleeps(monkeypatch, tmp_path, client, ticks=2)

    assert sleeps[0] == 45, (
        f"a server-supplied Retry-After must win over the exponential "
        f"formula's 240s: {sleeps}")
    assert sleeps[1] == 120, "must return to the normal cadence once it clears"


# --------------------------------------------------------------------------
# Task 18 -- the banked-solar ledger, wired through the REAL solar_tick
# (green.ledger_step itself is covered exhaustively in test_green.py). These
# tests are about the collector's own job: pulling soc/state/car_w/grid_w
# out of a real tick and persisting the result, not the ledger arithmetic.
# --------------------------------------------------------------------------

class _ControllableGridClient:
    """grid_power settable per call -- lets a test drive the real solar_tick
    through a rise, a drop, and a backdated gap."""

    def __init__(self, grid_w: float):
        self.grid_w = grid_w
        self.commands: list = []

    async def _get(self, path, ttl=0):
        return {"grid_power": self.grid_w, "solar_power": max(0.0, -self.grid_w)}

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        raise AssertionError("must not need a wake in this test")


def _ledger_view(soc, **overrides):
    view = {
        "charging_state": "Charging", "amps_actual": 24, "amps_max": 48,
        "volts": 240, "charge_amps": 24, "soc": soc, "limit": 80,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }
    view.update(overrides)
    return view


@pytest.mark.asyncio
async def test_ledger_first_tick_after_deployment_records_but_banks_nothing(tmp_path):
    """ledger_soc starts NULL (the migration default, see test_store.py's
    banked-solar migration test) -- the very first tick must record the
    observed soc and bank nothing, never assume a rise or a drop happened
    before the ledger was watching."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle", hold_s=10_000)

    client = _ControllableGridClient(grid_w=-6000.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    await collector.solar_tick(client, store_, "VIN1", _ledger_view(55), cfg, site_id=1)

    st = solar.load_state(db, "VIN1")
    assert st["solar_soc"] == 0
    assert st["ledger_soc"] == 55
    assert st["ledger_stale"] == 0
    store_.close()


@pytest.mark.asyncio
async def test_ledger_banks_a_rise_then_drains_a_drop_across_real_ticks(tmp_path):
    """End to end through the real solar_tick: a rise while engaged and
    exporting banks it, then a drop drains the bank proportionally --
    green.ledger_step's contract, driven by the collector's own soc/state/
    car_w/grid_w extraction rather than hand-fed pure-function inputs."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle", hold_s=10_000)

    client = _ControllableGridClient(grid_w=-6000.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")

    await collector.solar_tick(client, store_, "VIN1", _ledger_view(55), cfg, site_id=1)
    assert solar.load_state(db, "VIN1")["state"] == "charging", "premise: engaged by tick 1"

    # Rose 5 points while charging and exporting -- all 5 are solar.
    await collector.solar_tick(client, store_, "VIN1", _ledger_view(60), cfg, site_id=1)
    st = solar.load_state(db, "VIN1")
    assert st["solar_soc"] == pytest.approx(5.0)
    assert st["ledger_soc"] == 60

    # Unplugged and drove off 2 of the 60 points -- drains 2/60 of the bank.
    client.grid_w = 0.0
    view = _ledger_view(58, charging_state="Disconnected", amps_actual=None)
    await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)
    st = solar.load_state(db, "VIN1")
    assert st["solar_soc"] == pytest.approx(5.0 - 2 * (5.0 / 60))
    assert st["ledger_soc"] == 58
    store_.close()


@pytest.mark.asyncio
async def test_ledger_rise_while_grid_charging_does_not_bank(tmp_path):
    """A rise while IMPORTING (grid_w > 0) must not be credited to the sun,
    even though the controller may still be nominally 'charging' -- solar
    attribution (green.tick_solar_w) is what gates it, not state alone."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle", hold_s=10_000)

    client = _ControllableGridClient(grid_w=-6000.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    await collector.solar_tick(client, store_, "VIN1", _ledger_view(55), cfg, site_id=1)

    # Now importing more than the car draws -- attribution is exactly 0.
    client.grid_w = 9000.0
    await collector.solar_tick(client, store_, "VIN1", _ledger_view(60), cfg, site_id=1)
    st = solar.load_state(db, "VIN1")
    assert st["solar_soc"] == 0, "importing more than the car drew is not solar"
    assert st["ledger_soc"] == 60
    store_.close()


@pytest.mark.asyncio
async def test_ledger_flags_stale_after_a_gap_spanning_a_restart(tmp_path):
    """solar.last_tick_ts is read back from disk, not held in memory, so a
    gap that spans a process restart is measured correctly -- proven here by
    backdating the only logged tick so far, which is exactly what a real
    crash-and-restart would leave behind. Also moves the soc across that
    gap (55 -> 60): staleness needs BOTH a long gap and a change, not
    length alone (see green.ledger_step)."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle", hold_s=10_000)

    client = _ControllableGridClient(grid_w=-6000.0)
    # poll_asleep explicit (not just the collector's getattr fallback) so
    # this test pins the actual gap_threshold_s formula: max(2*1800, 3600).
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443",
                         poll_asleep=1800)
    await collector.solar_tick(client, store_, "VIN1", _ledger_view(55), cfg, site_id=1)
    assert solar.load_state(db, "VIN1")["ledger_stale"] == 0, "premise: first tick is not stale"

    db.execute("UPDATE solar_ticks SET ts = ts - ? WHERE vin = 'VIN1'",
               (2 * cfg.poll_asleep + 100,))
    db.commit()

    await collector.solar_tick(client, store_, "VIN1", _ledger_view(60), cfg, site_id=1)
    assert solar.load_state(db, "VIN1")["ledger_stale"] == 1
    store_.close()


@pytest.mark.asyncio
async def test_ledger_long_gap_with_unchanged_soc_is_not_stale_through_the_real_tick(tmp_path):
    """THE REGRESSION THAT MATTERS, wired through the real solar_tick: a car
    that slept through an ordinary idle-cadence gap with no soc change must
    not be flagged stale, even though the gap comfortably exceeds
    store.GAP_SECONDS (1800s) -- that constant is the SoC chart's, not this
    ledger's, and the car's own poll_asleep can legitimately be that long."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle", hold_s=10_000)

    client = _ControllableGridClient(grid_w=-6000.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443",
                         poll_asleep=1800)
    await collector.solar_tick(client, store_, "VIN1", _ledger_view(55), cfg, site_id=1)

    db.execute("UPDATE solar_ticks SET ts = ts - ? WHERE vin = 'VIN1'",
               (2 * cfg.poll_asleep + 100,))
    db.commit()

    # Same soc as before -- the car simply slept the whole gap.
    await collector.solar_tick(client, store_, "VIN1", _ledger_view(55), cfg, site_id=1)
    assert solar.load_state(db, "VIN1")["ledger_stale"] == 0
    store_.close()


# --------------------------------------------------------------------------
# Task 20 -- lifetime free miles driven, wired through the REAL solar_tick
# (green.free_miles_step itself is covered exhaustively in test_green.py).
# These tests are about the collector's own job: pulling odometer_mi out of
# a real tick and persisting the result alongside the banked-solar ledger,
# not the free-miles arithmetic.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_free_miles_first_tick_after_deployment_records_the_odometer_only(tmp_path):
    """ledger_odo starts NULL (the migration default) -- the very first
    tick must record the observed odometer and stamp free_miles_since, but
    accumulate nothing -- never assume driving happened before the ledger
    was watching."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle", hold_s=10_000)

    client = _ControllableGridClient(grid_w=-6000.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    before = int(time.time())
    await collector.solar_tick(
        client, store_, "VIN1", _ledger_view(55, odometer_mi=55_000.0), cfg, site_id=1)

    st = solar.load_state(db, "VIN1")
    assert st["ledger_odo"] == 55_000.0
    assert st["free_miles_driven"] == 0
    assert st["tracked_miles"] == 0
    assert st["free_miles_since"] is not None and st["free_miles_since"] >= before
    store_.close()


@pytest.mark.asyncio
async def test_free_miles_uses_the_ledgers_pre_tick_share_not_the_post_tick_one(tmp_path):
    """The tick that banks a rise must NOT credit that same window's miles
    with the share it just produced -- the solar fraction is only valid for
    the pack as it stood ACROSS the window just driven, not as it stands
    after this tick folds the rise in. Traced by hand: tick 1 records
    solar_soc=0/ledger_soc=55/ledger_odo=1000; tick 2 banks a 5-point rise
    (solar_soc 0 -> 5) while ALSO advancing the odometer 10 miles -- since
    the pre-tick share was 0/55 = 0%, free_miles_driven must stay exactly 0,
    not 10 * 5/55."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle", hold_s=10_000)

    client = _ControllableGridClient(grid_w=-6000.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")

    await collector.solar_tick(
        client, store_, "VIN1", _ledger_view(55, odometer_mi=1000.0), cfg, site_id=1)
    assert solar.load_state(db, "VIN1")["state"] == "charging", "premise: engaged by tick 1"

    await collector.solar_tick(
        client, store_, "VIN1", _ledger_view(60, odometer_mi=1010.0), cfg, site_id=1)
    st = solar.load_state(db, "VIN1")
    assert st["solar_soc"] == pytest.approx(5.0), "premise: tick 2 banked the rise"
    assert st["tracked_miles"] == pytest.approx(10.0)
    assert st["free_miles_driven"] == 0, "pre-tick share was 0/55, not the post-tick 5/55"
    store_.close()


@pytest.mark.asyncio
async def test_free_miles_credits_the_share_once_the_ledger_is_no_longer_empty(tmp_path):
    """A third tick, now WITH a nonzero pre-tick bank, actually credits free
    miles -- 6 miles driven at a pre-tick share of 5/60 (~8.33%)."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle", hold_s=10_000)

    client = _ControllableGridClient(grid_w=-6000.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")

    await collector.solar_tick(
        client, store_, "VIN1", _ledger_view(55, odometer_mi=1000.0), cfg, site_id=1)
    await collector.solar_tick(
        client, store_, "VIN1", _ledger_view(60, odometer_mi=1010.0), cfg, site_id=1)
    st = solar.load_state(db, "VIN1")
    assert (st["solar_soc"], st["ledger_soc"]) == (pytest.approx(5.0), 60), "premise from the prior test"

    # A third tick, still exporting, soc unchanged (no further bank/drain),
    # 6 more miles on the odometer.
    await collector.solar_tick(
        client, store_, "VIN1", _ledger_view(60, odometer_mi=1016.0), cfg, site_id=1)
    st = solar.load_state(db, "VIN1")
    assert st["tracked_miles"] == pytest.approx(16.0)
    assert st["free_miles_driven"] == pytest.approx(6.0 * (5.0 / 60.0))
    store_.close()


@pytest.mark.asyncio
async def test_free_miles_since_is_stamped_once_and_never_rewritten(tmp_path):
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle", hold_s=10_000)

    client = _ControllableGridClient(grid_w=-6000.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")

    await collector.solar_tick(
        client, store_, "VIN1", _ledger_view(55, odometer_mi=1000.0), cfg, site_id=1)
    first_stamp = solar.load_state(db, "VIN1")["free_miles_since"]
    assert first_stamp is not None

    await collector.solar_tick(
        client, store_, "VIN1", _ledger_view(60, odometer_mi=1010.0), cfg, site_id=1)
    await collector.solar_tick(
        client, store_, "VIN1", _ledger_view(60, odometer_mi=1016.0), cfg, site_id=1)
    assert solar.load_state(db, "VIN1")["free_miles_since"] == first_stamp
    store_.close()


@pytest.mark.asyncio
async def test_free_miles_ignores_a_view_missing_the_odometer(tmp_path):
    """A view without odometer_mi (Tesla omits keys rather than nulling
    them, per vehicle.py's own docstring) must leave free_miles_driven/
    tracked_miles/ledger_odo exactly as they were, same treatment as an
    unknown soc gets for the banked-solar ledger."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle", hold_s=10_000)

    client = _ControllableGridClient(grid_w=-6000.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")

    view = _ledger_view(55)
    view.pop("odometer_mi", None)
    assert "odometer_mi" not in view
    await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)

    st = solar.load_state(db, "VIN1")
    assert st["ledger_odo"] is None
    assert st["free_miles_driven"] == 0
    assert st["tracked_miles"] == 0
    assert st["free_miles_since"] is None
    store_.close()


# --------------------------------------------------------------------------
# Task 17b -- garage_arrival_tick: the one-shot arrival latch. should_open()
# itself is already exhaustively covered in test_garage.py; these tests are
# about the collector's own job -- computing ring membership, persisting the
# latch, and never touching the device when it shouldn't.
# --------------------------------------------------------------------------

from datetime import datetime               # noqa: E402  (grouped with the section it serves)
from zoneinfo import ZoneInfo               # noqa: E402

import garage                               # noqa: E402

TZ = "America/Denver"


def _closed_status(url):
    return {"garageDoorState": "Closed", "garageObstructed": False}


@pytest.mark.asyncio
async def test_garage_arrival_tick_arms_outside_and_fires_exactly_once_on_the_way_back_in(
    tmp_path, monkeypatch,
):
    """The simulated drive-out-and-back the brief asks for: outside the ring
    arms the latch, crossing back in while driving fires it once, and a
    further tick with the latch already spent must not re-fire."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    home.save(db, 40.0, -105.0, 100)
    solar.save_config(db, garage_url="http://fake-ratgdo", garage_auto_open=1,
                      garage_ring_m=500)

    calls = {"open": 0}
    monkeypatch.setattr(garage, "status", _closed_status)
    monkeypatch.setattr(garage, "open", lambda url: calls.__setitem__("open", calls["open"] + 1) or True)

    cfg = solar.load_config(db)

    away_view = {"lat": 41.0, "lon": -105.0, "shift": "D"}   # ~111 km out -- well outside 500 m
    await collector.garage_arrival_tick(db, "VIN1", away_view, cfg, home.load(db))
    assert solar.load_state(db, "VIN1")["garage_armed"] == 1, "must arm on leaving the ring"
    assert calls["open"] == 0

    home_view = {"lat": 40.0, "lon": -105.0, "shift": "D"}   # back at the home coordinate
    await collector.garage_arrival_tick(db, "VIN1", home_view, cfg, home.load(db))
    assert calls["open"] == 1, "must fire on the transition back in"
    assert solar.load_state(db, "VIN1")["garage_armed"] == 0, "must disarm on firing"

    await collector.garage_arrival_tick(db, "VIN1", home_view, cfg, home.load(db))
    assert calls["open"] == 1, "must not re-fire while disarmed"
    store_.close()


@pytest.mark.asyncio
async def test_garage_arrival_tick_never_fires_without_first_confirming_departure(
    tmp_path, monkeypatch,
):
    """A car that has always been inside the ring (never observed leaving)
    must never trigger an open, however many ticks pass -- mere presence is
    not arrival."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    home.save(db, 40.0, -105.0, 100)
    solar.save_config(db, garage_url="http://fake-ratgdo", garage_auto_open=1,
                      garage_ring_m=500)

    def boom(url):
        raise AssertionError("must never open without first confirming departure")
    monkeypatch.setattr(garage, "status", _closed_status)
    monkeypatch.setattr(garage, "open", boom)

    cfg = solar.load_config(db)
    view = {"lat": 40.0, "lon": -105.0, "shift": "D"}
    for _ in range(3):
        await collector.garage_arrival_tick(db, "VIN1", view, cfg, home.load(db))
    assert solar.load_state(db, "VIN1")["garage_armed"] == 0
    store_.close()


@pytest.mark.asyncio
async def test_garage_arrival_tick_noop_when_auto_open_disabled(tmp_path, monkeypatch):
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    home.save(db, 40.0, -105.0, 100)
    solar.save_config(db, garage_url="http://fake-ratgdo", garage_auto_open=0,
                      garage_ring_m=500)

    def boom(url):
        raise AssertionError("must never touch the device when auto-open is off")
    monkeypatch.setattr(garage, "status", boom)
    monkeypatch.setattr(garage, "open", boom)

    cfg = solar.load_config(db)
    view = {"lat": 41.0, "lon": -105.0, "shift": "D"}
    await collector.garage_arrival_tick(db, "VIN1", view, cfg, home.load(db))
    assert solar.load_state(db, "VIN1")["garage_armed"] == 0, "must not even arm when disabled"
    store_.close()


@pytest.mark.asyncio
async def test_garage_arrival_tick_freezes_on_unknown_location(tmp_path, monkeypatch):
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    home.save(db, 40.0, -105.0, 100)
    solar.save_config(db, garage_url="http://fake-ratgdo", garage_auto_open=1,
                      garage_ring_m=500)

    def boom(url):
        raise AssertionError("must never touch the device with no location")
    monkeypatch.setattr(garage, "status", boom)
    monkeypatch.setattr(garage, "open", boom)

    cfg = solar.load_config(db)
    view = {"lat": None, "lon": None, "shift": "D"}
    await collector.garage_arrival_tick(db, "VIN1", view, cfg, home.load(db))
    assert solar.load_state(db, "VIN1")["garage_armed"] == 0
    store_.close()


@pytest.mark.asyncio
async def test_garage_arrival_tick_treats_an_unreachable_device_as_not_closed(
    tmp_path, monkeypatch,
):
    """status() returning None must fail closed through should_open()'s own
    None handling, not through any special case here -- and, having never
    fired, the latch must stay armed for the next tick to retry."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    home.save(db, 40.0, -105.0, 100)
    solar.save_config(db, garage_url="http://fake-ratgdo", garage_auto_open=1,
                      garage_ring_m=500)
    solar.save_state(db, "VIN1", garage_armed=1)  # already armed, as if just departed

    def boom(url):
        raise AssertionError("must never open when status is unreachable")
    monkeypatch.setattr(garage, "status", lambda url: None)
    monkeypatch.setattr(garage, "open", boom)

    cfg = solar.load_config(db)
    view = {"lat": 40.0, "lon": -105.0, "shift": "D"}
    await collector.garage_arrival_tick(db, "VIN1", view, cfg, home.load(db))
    assert solar.load_state(db, "VIN1")["garage_armed"] == 1, "must stay armed -- never fired"
    store_.close()


# --------------------------------------------------------------------------
# Task 17b -- garage_scheduled_close_tick: the only path that ever closes the
# door automatically. safe_to_close() itself is unit-tested in
# test_garage.py; these tests are about the collector's orchestration --
# once-per-day, the two-read warning sequence, and never closing on a stale
# or missing read.
# --------------------------------------------------------------------------

def _this_hour() -> int:
    return datetime.now(ZoneInfo(TZ)).hour


def _today() -> str:
    return datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d")


async def _fake_sleep(_seconds) -> None:
    """A real coroutine standing in for asyncio.sleep -- `await` requires an
    actual awaitable, so a plain lambda returning None will not do."""


@pytest.mark.asyncio
async def test_garage_scheduled_close_fires_once_and_not_again_the_same_day(
    tmp_path, monkeypatch,
):
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, garage_url="http://fake-ratgdo",
                      garage_close_hour=_this_hour(), garage_close_warn_s=8)

    readings = iter([
        {"garageDoorState": "Open", "garageObstructed": False},  # pre-check
        {"garageDoorState": "Open", "garageObstructed": False},  # post-wait
    ])
    monkeypatch.setattr(garage, "status",
                        lambda url: next(readings, {"garageDoorState": "Closed", "garageObstructed": False}))
    monkeypatch.setattr(garage, "light_on", lambda url: True)
    close_calls = []
    monkeypatch.setattr(garage, "close", lambda url: close_calls.append(url) or True)

    sleeps = []
    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(collector.asyncio, "sleep", fake_sleep)

    cfg = solar.load_config(db)
    await collector.garage_scheduled_close_tick(db, "VIN1", cfg, TZ)
    assert close_calls == ["http://fake-ratgdo"]
    assert sleeps == [8], "must wait garage_close_warn_s before the second read"

    # Same hour, same day, ticked again -- must not close a second time.
    await collector.garage_scheduled_close_tick(db, "VIN1", cfg, TZ)
    assert close_calls == ["http://fake-ratgdo"], "must fire at most once per day"
    assert solar.load_state(db, "VIN1")["garage_last_close_day"] == _today()
    store_.close()


@pytest.mark.asyncio
async def test_garage_scheduled_close_aborts_when_obstruction_appears_during_the_wait(
    tmp_path, monkeypatch,
):
    """The whole reason for the second read: the door was clear when the
    light came on, then something entered the doorway during the wait."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, garage_url="http://fake-ratgdo",
                      garage_close_hour=_this_hour(), garage_close_warn_s=8)

    readings = iter([
        {"garageDoorState": "Open", "garageObstructed": False},
        {"garageDoorState": "Open", "garageObstructed": True},
    ])
    monkeypatch.setattr(garage, "status", lambda url: next(readings))
    monkeypatch.setattr(garage, "light_on", lambda url: True)

    def boom(url):
        raise AssertionError("must never close an obstructed door")
    monkeypatch.setattr(garage, "close", boom)
    monkeypatch.setattr(collector.asyncio, "sleep", _fake_sleep)

    cfg = solar.load_config(db)
    await collector.garage_scheduled_close_tick(db, "VIN1", cfg, TZ)
    assert solar.load_state(db, "VIN1")["garage_last_close_day"] == _today(), (
        "must still stamp the day even when it aborts")
    store_.close()


@pytest.mark.asyncio
async def test_garage_scheduled_close_aborts_when_door_no_longer_open_after_the_wait(
    tmp_path, monkeypatch,
):
    """Someone (or something else entirely) closed it during the wait."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, garage_url="http://fake-ratgdo",
                      garage_close_hour=_this_hour(), garage_close_warn_s=8)

    readings = iter([
        {"garageDoorState": "Open", "garageObstructed": False},
        {"garageDoorState": "Closed", "garageObstructed": False},
    ])
    monkeypatch.setattr(garage, "status", lambda url: next(readings))
    monkeypatch.setattr(garage, "light_on", lambda url: True)

    def boom(url):
        raise AssertionError("must never close a door that is no longer Open")
    monkeypatch.setattr(garage, "close", boom)
    monkeypatch.setattr(collector.asyncio, "sleep", _fake_sleep)

    cfg = solar.load_config(db)
    await collector.garage_scheduled_close_tick(db, "VIN1", cfg, TZ)
    store_.close()


@pytest.mark.asyncio
async def test_garage_scheduled_close_skips_when_the_door_was_never_open(tmp_path, monkeypatch):
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, garage_url="http://fake-ratgdo", garage_close_hour=_this_hour())

    monkeypatch.setattr(garage, "status", _closed_status)

    def boom(url):
        raise AssertionError("must never warn or close a door that was never open")
    monkeypatch.setattr(garage, "light_on", boom)
    monkeypatch.setattr(garage, "close", boom)

    cfg = solar.load_config(db)
    await collector.garage_scheduled_close_tick(db, "VIN1", cfg, TZ)
    assert solar.load_state(db, "VIN1")["garage_last_close_day"] == _today(), (
        "must still stamp the day so a closed door does not get re-checked all hour"
    )
    store_.close()


@pytest.mark.asyncio
async def test_garage_scheduled_close_skips_when_the_device_is_unreachable_up_front(
    tmp_path, monkeypatch,
):
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, garage_url="http://fake-ratgdo", garage_close_hour=_this_hour())

    monkeypatch.setattr(garage, "status", lambda url: None)

    def boom(url):
        raise AssertionError("must never warn or close when the first read fails")
    monkeypatch.setattr(garage, "light_on", boom)
    monkeypatch.setattr(garage, "close", boom)

    cfg = solar.load_config(db)
    await collector.garage_scheduled_close_tick(db, "VIN1", cfg, TZ)
    assert solar.load_state(db, "VIN1")["garage_last_close_day"] == _today()
    store_.close()


@pytest.mark.asyncio
async def test_garage_scheduled_close_aborts_when_the_device_goes_unreachable_during_the_wait(
    tmp_path, monkeypatch,
):
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, garage_url="http://fake-ratgdo",
                      garage_close_hour=_this_hour(), garage_close_warn_s=8)

    readings = iter([{"garageDoorState": "Open", "garageObstructed": False}, None])
    monkeypatch.setattr(garage, "status", lambda url: next(readings))
    monkeypatch.setattr(garage, "light_on", lambda url: True)

    def boom(url):
        raise AssertionError("must never close on an unreadable post-wait status")
    monkeypatch.setattr(garage, "close", boom)
    monkeypatch.setattr(collector.asyncio, "sleep", _fake_sleep)

    cfg = solar.load_config(db)
    await collector.garage_scheduled_close_tick(db, "VIN1", cfg, TZ)
    store_.close()


@pytest.mark.asyncio
async def test_garage_scheduled_close_noop_outside_the_configured_hour(tmp_path, monkeypatch):
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    other_hour = (_this_hour() + 12) % 24
    solar.save_config(db, garage_url="http://fake-ratgdo", garage_close_hour=other_hour)

    def boom(*a, **k):
        raise AssertionError("must never touch the device outside the configured hour")
    monkeypatch.setattr(garage, "status", boom)

    cfg = solar.load_config(db)
    await collector.garage_scheduled_close_tick(db, "VIN1", cfg, TZ)
    assert solar.load_state(db, "VIN1")["garage_last_close_day"] is None
    store_.close()


@pytest.mark.asyncio
async def test_garage_scheduled_close_noop_when_hour_is_unset(tmp_path, monkeypatch):
    """garage_close_hour defaults to NULL -- the schedule is off until the
    owner sets one, same convention as deadline_hour."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, garage_url="http://fake-ratgdo")

    def boom(*a, **k):
        raise AssertionError("must never touch the device with no close hour set")
    monkeypatch.setattr(garage, "status", boom)

    cfg = solar.load_config(db)
    await collector.garage_scheduled_close_tick(db, "VIN1", cfg, TZ)
    store_.close()


@pytest.mark.asyncio
async def test_garage_scheduled_close_stamps_the_day_before_attempting_anything(
    tmp_path, monkeypatch,
):
    """Stamp first, act second: a crash mid-sequence must not leave the door
    retrying against a possibly-obstructed door for the rest of the day,
    including across a process restart."""
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, garage_url="http://fake-ratgdo", garage_close_hour=_this_hour())

    def boom(url):
        raise RuntimeError("simulated crash mid-sequence")
    monkeypatch.setattr(garage, "status", boom)

    cfg = solar.load_config(db)
    with pytest.raises(RuntimeError):
        await collector.garage_scheduled_close_tick(db, "VIN1", cfg, TZ)

    assert solar.load_state(db, "VIN1")["garage_last_close_day"] == _today()
    store_.close()


# --------------------------------------------------------------------------
# Task 17b wiring proof -- both garage ticks must actually be called from
# inside run(), every iteration, independent of solar. Mirrors the existing
# call-count style used for recover()'s startup latch above.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_calls_both_garage_ticks_every_iteration(tmp_path, monkeypatch):
    db_path = tmp_path / "car.db"
    seed = Store(db_path)
    seed.close()

    monkeypatch.setattr(collector.settings, "db_file", db_path)
    monkeypatch.setattr(collector, "TeslaClient", lambda settings: _HomeChargingClient())

    arrival_calls: list[int] = []
    close_calls: list[int] = []

    async def fake_arrival(db, vin, view, cfg, home_cfg):
        arrival_calls.append(1)

    async def fake_close(db, vin, cfg, tz):
        close_calls.append(1)

    monkeypatch.setattr(collector, "garage_arrival_tick", fake_arrival)
    monkeypatch.setattr(collector, "garage_scheduled_close_tick", fake_close)

    sleep_count = 0

    async def fake_sleep(seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 3:
            raise StopTest()
    monkeypatch.setattr(collector.asyncio, "sleep", fake_sleep)

    with pytest.raises(StopTest):
        await collector.run()

    assert len(close_calls) == 3, "the scheduled-close check must run every tick"
    assert len(arrival_calls) == 3, "the arrival check must run whenever a view exists"


# --------------------------------------------------------------------------
# Task 21 -- ADOPT: a second, distinct way into "charging", for a car found
# ALREADY drawing power the controller did not command (it auto-started on
# plug-in, or the owner started it from the Tesla app). Driven as a JOURNEY
# through the REAL solar_tick, not just the pure state machine -- the gap
# this closes was invisible to state-level tests because they check states,
# not sequences.
#
# Numbers are the observed live reading (12:29, the first real solar day):
# solar 4.26 kW, car 11.28 kW (48 A x 235 V, started by the car), grid +16.76
# kW import, surplus -5.48 kW. Modelled here at a round 240 V for the same
# reason every other fixture in this file uses it.
# --------------------------------------------------------------------------

class _AdoptedCarClient:
    """A steady, deep grid import that never lets up -- the car auto-started
    at 48 A against solar that cannot support it, unchanging tick to tick so
    the journey below exercises the breach dwell and grace timeout on their
    own terms rather than a moving target."""

    def __init__(self):
        self.commands: list[tuple[str, dict]] = []

    async def _get(self, path, ttl=0):
        return {"grid_power": 16760.0, "solar_power": 4260.0}

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        raise AssertionError("must never wake a car that is already awake and charging")


@pytest.mark.asyncio
async def test_adopts_a_charge_the_car_started_and_restores_on_stop(tmp_path):
    """The full arc: adopt (recording the owner's 48 A BEFORE ever touching
    amps, no charge_start), bypass the ramp on the way down (reducing draw
    is always safe), ride the two-tick breach dwell into grace, and on
    grace's expiry stop the charge and restore exactly 48 A -- with no
    charge_start issued anywhere in the whole sequence, because the car
    started this charge, not the controller.
    """
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)      # period_s=120, ramp_a=8, min_a=5,
    home.save(db, 40.0, -105.0, 100)       # grace_s=180 -- all defaults

    # solar_state is left at ITS defaults (idle, dirty=0, original_amps=None)
    # -- exactly what the owner's DB looks like the moment solar mode is
    # enabled while the car is already mid-charge. No hold_s seeding: ADOPT
    # needs none.

    client = _AdoptedCarClient()
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    view = {
        "charging_state": "Charging", "amps_actual": 48, "amps_max": 48,
        "volts": 240, "charge_amps": 48, "soc": 60, "limit": 90,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }

    # --- tick 1: ADOPT, record the original, bypass the ramp on the way down
    state, _ = await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)
    assert state == "charging"
    st = solar.load_state(db, "VIN1")
    assert st["dirty"] == 1
    assert st["original_amps"] == 48, "the owner's amps must be recorded BEFORE any write"
    assert st["original_limit"] == 90

    amps_commands = [c for c in client.commands if c[0] == "set_charging_amps"]
    assert amps_commands, "the adoption tick must correct amps immediately, not wait"
    first_write = amps_commands[0][1]["charging_amps"]
    assert first_write != 48 - 8, "must NOT be limited to a single ramp_a=8 step"
    assert first_write == 5, "the bypass drops straight to the floor, as grace does"

    # --- tick 2: first breach tick -- dwelling, no new action yet ----------
    state, _ = await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)
    assert state == "charging"
    assert solar.load_state(db, "VIN1")["breach_ticks"] == 1

    # --- tick 3: second consecutive breach tick -> grace at min_a ----------
    state, _ = await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)
    assert state == "grace"

    # --- ride out the dip, then give up -> charge_stop AND restore 48 A ----
    # Grace is now bounded by ENERGY, not by a bare 180 s timer: the car sits
    # at its 1.2 kW floor and every watt of it is imported here, so a 150 Wh
    # budget buys 450 s -- four 120 s ticks -- before the machine concedes.
    # That is the ride-through working: a real compressor cycle outlasts three
    # minutes, which is exactly how 4.7 kW came to be exported on 2026-07-27.
    for _ in range(8):
        state, _ = await collector.solar_tick(client, store_, "VIN1", view,
                                              cfg, site_id=1)
        if state == "stopped":
            break
        assert state == "grace", f"expected to still be riding it out, got {state}"
    assert state == "stopped", "the energy budget must eventually give up"

    spent = solar.load_state(db, "VIN1")["grace_wh"]
    assert spent == 0.0, "the budget must reset when grace ends, not carry over"

    stops = [c for c in client.commands if c[0] == "charge_stop"]
    assert len(stops) == 1
    restores = [c for c in client.commands if c[0] == "set_charging_amps"
                and c[1]["charging_amps"] == 48]
    assert restores, f"original_amps must be restored to 48: {client.commands}"

    final = solar.load_state(db, "VIN1")
    assert final["dirty"] == 0
    assert final["original_amps"] is None

    # --- the whole point: the controller never issues its own charge_start -
    starts = [c for c in client.commands if c[0] == "charge_start"]
    assert starts == [], f"the car started this charge, not us: {client.commands}"


@pytest.mark.asyncio
async def test_without_adopt_the_controller_sits_in_idle_watching_it_import(tmp_path):
    """Discriminating check: covers exactly the defect this task closes. With
    no ADOPT transition, a car found already charging is invisible to a
    machine that only ever enters "charging" from a sustained-surplus START
    -- surplus here is deeply negative, so START never fires either, and the
    machine sits in "idle" forever while 48 A keeps flowing from the grid.

    This test drives a car that is NOT actually charging (charging_state
    outside the live-charging set) -- exactly what the pre-Task-21 machine
    could see, since it had no `car_charging` signal at all -- to show the
    machine has no OTHER way into "charging" for a car it did not itself
    start. The report for this task carries the verbatim red/green run
    captured by reverting the ADOPT branch in solar.advance() and re-running
    test_adopts_a_charge_the_car_started_and_restores_on_stop above, which
    IS wired to car_charging and fails without it.
    """
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)

    client = _AdoptedCarClient()
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    view = {
        "charging_state": "Stopped", "amps_actual": None, "amps_max": 48,
        "volts": 240, "charge_amps": 48, "soc": 60, "limit": 90,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }

    for _ in range(5):
        state, _ = await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)
        assert state == "idle"

    assert client.commands == [], (
        f"nothing should ever be commanded while genuinely idle: {client.commands}")
    store_.close()


class _SolarSiteClient:
    """A site whose grid meter actually responds to the car, unlike the fixed
    fixtures above.

    The car IS behind the grid CT on this installation -- proven live on
    2026-07-27, when the meter read +11,783 W while the car drew 11,328 W. So
    grid = house + car - solar, and any amps the controller commands feed
    straight back into the next tick's measurement. Without that coupling a
    test cannot tell a controller that converges from one that overshoots.
    """

    def __init__(self, house_w: float, solar_w: float, volts: int = 240):
        self.commands: list[tuple[str, dict]] = []
        self.house_w = house_w
        self.solar_w = solar_w
        self.volts = volts
        self.amps = 0          # what the car is actually drawing
        self.charging = False

    @property
    def car_w(self) -> float:
        return self.amps * self.volts if self.charging else 0.0

    async def _get(self, path, ttl=0):
        return {"grid_power": self.house_w + self.car_w - self.solar_w,
                "solar_power": self.solar_w}

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        if name == "set_charging_amps":
            self.amps = params["charging_amps"]
        elif name == "charge_start":
            self.charging = True
            if self.amps == 0:
                self.amps = 48        # the car resumes at its standing rate
        elif name == "charge_stop":
            self.charging = False
            self.amps = 0
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        # Recorded, not silently swallowed: a wake is the most expensive
        # request this system can make ($0.02 vs $0.001 for a command), so a
        # test that cannot see one cannot police it.
        self.commands.append(("wake_up", {}))
        return {"state": "online"}

    def view(self) -> dict:
        return {"charging_state": "Charging" if self.charging else "Stopped",
                "amps_actual": self.amps if self.charging else 0,
                "amps_max": 48, "volts": self.volts, "charge_amps": 48,
                "soc": 60, "limit": 90, "lat": 40.0, "lon": -105.0,
                "fast_charger_present": False, "fast_charger": None}


@pytest.mark.asyncio
async def test_engaging_opens_at_the_measured_rate_not_the_owners_standing_amps(tmp_path):
    """T0.1 + T0.2. Covers the ANCHOR defect, reproduced against the shipped
    collector on 2026-07-27 (340 Wh imported per engagement).

    collector.py anchored the integral law to `charge_amps` -- the owner's
    standing 48 A -- whenever the car was not already charging, because
    `amps_actual` is 0 and 0 is falsy. control() then returned
    clamp(48 + step, 5, 48) = 48, the already-holds-this-value guard
    suppressed the write, and the car opened the solar charge at FULL RATE
    into whatever surplus existed, ramping down 8 A per tick while importing
    the whole way.

    The fix is the owner's stated requirement: hold the rate at zero until
    the excess is measured, then go straight to it. `idle` already IS zero
    draw -- no command, no contactor cycle, no wake -- so with current_a
    correctly anchored at 0, grid_w already measures the house exactly and
    the very tick that engages can jump open-loop to the right answer.

    6 kW of surplus at 240 V is 25 A. The engagement must land there, not at
    48 A, and not at the 5 A a ramp-limited step from zero would give.
    """
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    # Seed the sustained-surplus dwell so tick 1 is the engagement itself.
    solar.save_state(db, "VIN1", state="idle", hold_s=solar.CONFIG_DEFAULTS["start_hold_s"])

    client = _SolarSiteClient(house_w=1700.0, solar_w=7700.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")

    state, _ = await collector.solar_tick(client, store_, "VIN1", client.view(),
                                          cfg, site_id=1)
    assert state == "charging"

    writes = [c[1]["charging_amps"] for c in client.commands
              if c[0] == "set_charging_amps"]
    assert writes, f"the engagement must command a rate: {client.commands}"

    first = writes[0]
    assert first != 48, (
        "REGRESSION: the controller opened at the owner's standing 48 A. "
        "That is 11.3 kW into 6 kW of surplus -- ~340 Wh of grid import per "
        f"engagement on the feature meant to import nothing. commands={client.commands}")
    assert 24 <= first <= 26, (
        f"6 kW / 240 V = 25 A; a clean measurement should land there, got {first} A. "
        "A value of 5-8 A means the jump was ramp-limited from zero, which is "
        "safe but wastes the surplus it just measured.")

    # And it must SETTLE there rather than oscillate: replay two more ticks
    # against a meter that now sees the car.
    for _ in range(2):
        state, _ = await collector.solar_tick(client, store_, "VIN1",
                                              client.view(), cfg, site_id=1)
    assert state == "charging", "must hold the engagement, not breach straight back out"
    grid_w = client.house_w + client.car_w - client.solar_w
    assert abs(grid_w) < 700, (
        f"settled {grid_w:.0f} W off zero; the servo should null the meter")
    store_.close()


@pytest.mark.asyncio
async def test_a_downward_correction_is_never_ramp_limited(tmp_path):
    """T1.2. Reducing draw is always safe, so it should never be rationed.

    An AC compressor starting is a ~6 kW step. Ramp-limited at 8 A per tick,
    a car at 48 A takes five ticks -- ten minutes at the deployed period -- to
    get down to where the meter already says it should be, importing the whole
    way. Upward moves stay ramp-limited, because slamming into a surplus that
    may not still be there is a real risk; downward moves carry no such risk.

    Fast-down / slow-up is also what keeps the loop stable: the aggressive
    direction is the one that can only reduce error.
    """
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="charging", dirty=1,
                     original_amps=48, original_limit=90)

    # Car at 48 A into 8 kW of sun and a 2 kW house: the site is importing
    # 5.5 kW and the loop wants ~25 A. Deliberately NOT a floor breach --
    # that path dwells two ticks before acting and would write nothing here.
    client = _SolarSiteClient(house_w=2000.0, solar_w=8000.0)
    client.charging = True
    client.amps = 48
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")

    await collector.solar_tick(client, store_, "VIN1", client.view(), cfg, site_id=1)

    writes = [c[1]["charging_amps"] for c in client.commands
              if c[0] == "set_charging_amps"]
    assert writes, f"an import must produce a correction: {client.commands}"
    assert writes[0] != 40, (
        "REGRESSION: took a single ramp_a step from 48 to 40, leaving the car "
        "importing for another two ticks while the meter already knew the answer")
    assert 24 <= writes[0] <= 26, (
        f"expected ~25 A in one move, got {writes[0]} A")
    store_.close()


async def _no_sleep(_seconds):
    """The wake path sleeps 5 s waiting for the car; tests need not."""
    return None


class _AsleepCarClient(_SolarSiteClient):
    """A site with real surplus and a car that is asleep until woken.

    vehicle() reports "asleep", so poll_once returns no view at all -- which
    is exactly the condition under which the solar loop used to do nothing.
    """

    def __init__(self, house_w, solar_w):
        super().__init__(house_w, solar_w)
        self.awake = False

    async def vehicle(self, vin):
        return {"state": "online" if self.awake else "asleep"}

    async def vehicle_data(self, vin):
        raise AssertionError("must not pay for vehicle_data while asleep")

    async def wake_up(self, vin):
        self.commands.append(("wake_up", {}))
        self.awake = True
        return {"state": "online"}


@pytest.mark.asyncio
async def test_a_sleeping_plugged_in_car_is_woken_for_sustained_surplus(
        tmp_path, monkeypatch):
    """The gap observed live on 2026-07-28.

    The car sat plugged in at 38% against a 91% limit -- 53 points of
    headroom -- from 06:55 while the sun came up, and the controller never
    engaged. solar_tick only runs when vehicle_data returns a view, and a
    sleeping car returns none, so the machine could never reach the `wake`
    action it already had.

    Waking must still be EARNED: restart_hold_s of sustained surplus, not one
    hopeful reading. A manual wake issued against a 1,720 W surplus on
    2026-07-28 landed six minutes later against -120 W, having bought
    nothing -- which is precisely what the hold exists to prevent.
    """
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="stopped", hold_s=0)

    client = _AsleepCarClient(house_w=1400.0, solar_w=7000.0)   # 5.6 kW spare
    # The stored snapshot is all the loop has to reason from.
    store_.record({"charging_state": "Stopped", "amps_actual": 0, "charging": 0,
                   "charge_amps": 48, "amps_max": 48, "volts": 240,
                   "soc": 38, "limit": 91, "lat": 40.0, "lon": -105.0,
                   "fast_charger_present": False, "fast_charger": None,
                   "vin": "VIN1", "sampled_at": int(time.time())}, at_home=True)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    # Tick 1: surplus is there but unproven -- accumulate, spend nothing.
    state, _ = await collector.solar_tick(client, store_, "VIN1", None, cfg,
                                          site_id=1)
    assert ("wake_up", {}) not in client.commands, (
        "a single reading must never buy a wake -- that is the mistake the "
        "sustained hold exists to prevent")
    assert solar.load_state(db, "VIN1")["hold_s"] > 0, "the hold must accumulate"

    # restart_hold_s=300 against a 120 s period: the hold is compared as
    # carried in, so it takes four ticks. Loop rather than hardcode the count.
    for _ in range(6):
        if ("wake_up", {}) in client.commands:
            break
        state, _ = await collector.solar_tick(client, store_, "VIN1", None,
                                              cfg, site_id=1)
    assert ("wake_up", {}) in client.commands, (
        f"sustained surplus must wake a plugged-in, hungry car: {client.commands}")
    assert state == "charging"
    starts = [c for c in client.commands if c[0] == "charge_start"]
    assert starts, f"and then actually start it: {client.commands}"
    store_.close()


@pytest.mark.asyncio
async def test_a_sleeping_car_is_left_alone_when_there_is_nothing_to_gain(tmp_path):
    """Discriminating half: same sleeping car, same sun, but already full.
    Nothing should be spent -- no wake, no command, no vehicle_data.
    """
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="stopped", hold_s=0)

    client = _AsleepCarClient(house_w=1400.0, solar_w=7000.0)
    store_.record({"charging_state": "Stopped", "amps_actual": 0, "charging": 0,
                   "charge_amps": 48, "amps_max": 48, "volts": 240,
                   "soc": 91, "limit": 91, "lat": 40.0, "lon": -105.0,
                   "fast_charger_present": False, "fast_charger": None,
                   "vin": "VIN1", "sampled_at": int(time.time())}, at_home=True)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")

    for _ in range(4):
        await collector.solar_tick(client, store_, "VIN1", None, cfg, site_id=1)

    assert client.commands == [], (
        f"a full car must never be woken for sunshine: {client.commands}")
    store_.close()


@pytest.mark.asyncio
async def test_an_awake_car_starts_on_the_first_qualifying_tick_and_is_not_woken(tmp_path):
    """Owner's call: stopped -> charging should not wait.

    The restart hold existed to avoid spending a $0.02 wake on a surplus that
    might not last. But it charged that delay on EVERY restart, including the
    common case where the car is already online and starting costs a $0.001
    command. Eight minutes of surplus was being discarded to insure against a
    cost that was not being incurred.

    Two changes, together: restart_hold_s may now be 0, and `wake` is skipped
    when the car is already online. A sleeping car still pays the hold, since
    that is the case the hold was actually protecting.
    """
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1, restart_hold_s=0)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="stopped", hold_s=0)

    client = _SolarSiteClient(house_w=1400.0, solar_w=7000.0)   # 5.6 kW spare
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")

    state, _ = await collector.solar_tick(client, store_, "VIN1", client.view(),
                                          cfg, site_id=1)

    assert state == "charging", (
        "an awake car with real surplus must start on the first qualifying "
        "tick, not eight minutes later")
    starts = [c for c in client.commands if c[0] == "charge_start"]
    assert starts, f"and must actually be started: {client.commands}"
    wakes = [c for c in client.commands if c[0] == "wake_up"]
    assert wakes == [], (
        f"the car was already online -- waking it costs $0.02 for nothing: "
        f"{client.commands}")
    store_.close()


class _WatchLoopClient:
    """A sleeping car and a site meter, recording which KIND of request each
    tick makes. The distinction is the whole point: a site read is one
    billable request, a vehicle state check is another, and a watch tick
    should make only the former."""

    def __init__(self, calls, grid_w=-5600.0):
        self.calls = calls
        self.grid_w = grid_w

    async def resolve_vin(self):
        return "VIN1"

    async def energy_sites(self):
        return [{"energy_site_id": 1}]

    async def vehicle(self, vin):
        self.calls.append("vehicle")
        return {"state": "asleep"}

    async def vehicle_data(self, vin, *a, **k):
        self.calls.append("vehicle_data")
        raise AssertionError("a sleeping car must never be paid for")

    async def _get(self, path, ttl=0):
        self.calls.append("site")
        return {"grid_power": self.grid_w, "solar_power": 7000.0}

    async def command(self, vin, name, params):
        self.calls.append(f"cmd:{name}")
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        self.calls.append("wake")
        return {"state": "online"}

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_the_watch_polls_the_meter_without_touching_the_car(monkeypatch, tmp_path):
    """A sleeping, plugged-in, hungry car is watched via the SITE meter alone.

    Only the meter can say whether there is anything worth waking for, and it
    is a site request -- the vehicle need not be disturbed at all until the
    surplus actually crosses. Halving the per-tick cost is what makes a tight
    cadence affordable: at one request per tick a 300 s watch costs less than
    the 1800 s two-request poll it replaces, while responding 6x sooner.
    """
    calls: list[str] = []
    db_path = tmp_path / "car.db"

    seed = Store(db_path)
    solar.save_config(seed._db, enabled=1, watch_s=300, restart_hold_s=99999)
    home.save(seed._db, 40.0, -105.0, 100)
    solar.save_state(seed._db, "VIN1", state="stopped", hold_s=0)
    seed.record({"charging_state": "Stopped", "amps_actual": 0, "charging": 0,
                 "charge_amps": 48, "amps_max": 48, "volts": 240,
                 "soc": 38, "limit": 91, "lat": 40.0, "lon": -105.0,
                 "fast_charger_present": False, "fast_charger": None,
                 "vin": "VIN1", "sampled_at": int(time.time())}, at_home=True)
    seed.close()

    monkeypatch.setattr(collector.settings, "db_file", db_path)
    monkeypatch.setattr(collector, "TeslaClient",
                        lambda settings: _WatchLoopClient(calls))

    slept: list[int] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 4:
            raise StopTest()
    monkeypatch.setattr(collector.asyncio, "sleep", fake_sleep)

    with pytest.raises(StopTest):
        await collector.run()

    # The bootstrap tick still checks state once; every watch tick after it
    # must read the meter and nothing else.
    assert calls.count("site") >= 3, f"the meter must be polled each tick: {calls}"
    assert calls.count("vehicle") <= 1, (
        f"a watch tick must not pay for a vehicle state check: {calls}")
    assert "vehicle_data" not in calls

    # And it must do so at the WATCH cadence, not the 1800 s asleep poll.
    assert slept[-1] == 300, (
        f"watch ticks must use watch_s, got {slept}")
    store_ = Store(db_path)
    store_.close()


class _StartFailsClient(_SolarSiteClient):
    """Wakes fine, but charge_start is refused -- the live 2026-07-28 failure,
    where a command sent seconds behind wake_up came back HTTP 500 because the
    car was still coming out of sleep."""

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        if name == "charge_start":
            return 500, {}
        if name == "set_charging_amps":
            self.amps = params["charging_amps"]
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        self.commands.append(("wake_up", {}))
        return {"state": "online"}


@pytest.mark.asyncio
async def test_a_refused_charge_start_does_not_strand_the_machine(tmp_path, monkeypatch):
    """Observed live 2026-07-28 09:30.

    The watch fired, woke the car, and charge_start came back HTTP 500 -- but
    the machine advanced to "charging" regardless. That strands it three ways
    at once: it believes it is servoing a charge that does not exist, it
    writes amps to a car that is not drawing, and because "charging" is
    neither idle nor stopped the meter watch stops too, so nothing re-checks
    for a full poll_asleep. The car sat at 9 A commanded, 0 A drawn, with the
    next look half an hour away.

    A start that did not start must leave the machine where it was.
    """
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1, restart_hold_s=0)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="stopped", hold_s=0)
    store_.record({"charging_state": "Stopped", "amps_actual": 0, "charging": 0,
                   "charge_amps": 48, "amps_max": 48, "volts": 240,
                   "soc": 38, "limit": 91, "lat": 40.0, "lon": -105.0,
                   "fast_charger_present": False, "fast_charger": None,
                   "vin": "VIN1", "sampled_at": int(time.time())}, at_home=True)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    client = _StartFailsClient(house_w=1400.0, solar_w=7000.0)
    state, _ = await collector.solar_tick(client, store_, "VIN1", None, cfg,
                                          site_id=1)

    assert state != "charging", (
        "a refused charge_start must not leave the machine believing it is "
        "charging -- that disables the watch and strands it for a full "
        "poll_asleep")
    st = solar.load_state(db, "VIN1")
    assert st["state"] != "charging"
    # The owner's 48 A must come back: amps were written before the start was
    # refused, so the car is left holding the controller's value otherwise.
    restores = [c for c in client.commands
                if c[0] == "set_charging_amps" and c[1]["charging_amps"] == 48]
    assert restores, f"the owner's amps must be restored: {client.commands}"
    assert st["dirty"] == 0, "and the dirty flag cleared once restored"
    store_.close()


class _AwakeIdleLoopClient(_WatchLoopClient):
    """Car online and idle, plugged in, with surplus below the start threshold
    -- the machine sits in idle waiting for it to cross."""

    async def vehicle(self, vin):
        self.calls.append("vehicle")
        return {"state": "online"}

    async def vehicle_data(self, vin, *a, **k):
        self.calls.append("vehicle_data")
        return {"charge_state": {"charging_state": "Stopped",
                                 "charger_actual_current": 0,
                                 "charge_amps": 48,
                                 "charge_current_request_max": 48,
                                 "charger_voltage": 240,
                                 "battery_level": 38,
                                 "charge_limit_soc": 91,
                                 "fast_charger_present": False},
                "drive_state": {"latitude": 40.0, "longitude": -105.0,
                                "timestamp": 0},
                "vehicle_state": {"odometer": 1000}}


@pytest.mark.asyncio
async def test_an_awake_idle_car_also_waits_at_the_watch_cadence(monkeypatch, tmp_path):
    """The same complaint in the other state.

    A sleeping car falls to poll_asleep; an awake IDLE one falls to poll_idle,
    because "idle" is not in ENGAGED_STATES. Both are 1800 s, and both are the
    same situation -- nothing to servo, just a threshold to notice. Half an
    hour of standing surplus either way.
    """
    calls: list[str] = []
    db_path = tmp_path / "car.db"
    seed = Store(db_path)
    solar.save_config(seed._db, enabled=1, watch_s=300)
    home.save(seed._db, 40.0, -105.0, 100)
    solar.save_state(seed._db, "VIN1", state="idle", hold_s=0)
    seed.close()

    monkeypatch.setattr(collector.settings, "db_file", db_path)
    # Surplus BELOW the 1300 W floor, so it stays in idle rather than starting.
    monkeypatch.setattr(collector, "TeslaClient",
                        lambda settings: _AwakeIdleLoopClient(calls, grid_w=-400.0))

    slept: list[int] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 3:
            raise StopTest()
    monkeypatch.setattr(collector.asyncio, "sleep", fake_sleep)

    with pytest.raises(StopTest):
        await collector.run()

    assert slept[-1] == 300, (
        f"an awake car waiting for surplus must poll at watch_s, not "
        f"poll_idle: {slept}")


@pytest.mark.asyncio
async def test_the_watch_stands_down_after_dark_and_resumes_at_dawn(
        monkeypatch, tmp_path):
    """Measured on this site: 33 watch ticks a night, ~$1.98/month, spent
    asking whether the sun was up at 1 a.m.

    The pair matters more than either half. Backing off is easy; the risk is
    backing off and never coming back, which would silently disable the whole
    feature some morning and look exactly like it working.
    """
    calls: list[str] = []
    db_path = tmp_path / "car.db"
    seed = Store(db_path)
    solar.save_config(seed._db, enabled=1, watch_s=300)
    home.save(seed._db, 40.0, -105.0, 100)
    solar.save_state(seed._db, "VIN1", state="stopped", hold_s=0)
    seed.record({"charging_state": "Stopped", "amps_actual": 0, "charging": 0,
                 "charge_amps": 48, "amps_max": 48, "volts": 240,
                 "soc": 38, "limit": 91, "lat": 40.0, "lon": -105.0,
                 "fast_charger_present": False, "fast_charger": None,
                 "vin": "VIN1", "sampled_at": int(time.time())}, at_home=True)
    seed.close()

    night = _WatchLoopClient(calls, grid_w=1800.0)      # importing, no sun
    night.solar_w = 0.0

    async def dark_get(path, ttl=0):
        calls.append("site")
        return {"grid_power": 1800.0, "solar_power": 0.0}
    night._get = dark_get

    monkeypatch.setattr(collector.settings, "db_file", db_path)
    monkeypatch.setattr(collector, "TeslaClient", lambda settings: night)

    slept: list[int] = []

    # solar_ticks is keyed (vin, ts) with INSERT OR REPLACE, so ticks that all
    # land in the same wall-clock second overwrite each other and only ONE row
    # ever exists -- is_dark would then never see DARK_TICKS readings and
    # would fail open forever. Advance the clock so each tick logs its own row,
    # exactly as it does in production at a 300 s cadence.
    clock = [int(time.time())]
    monkeypatch.setattr(collector.time, "time", lambda: clock[0])

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock[0] += max(int(seconds), 1)
        if len(slept) >= 5:
            raise StopTest()
    monkeypatch.setattr(collector.asyncio, "sleep", fake_sleep)

    with pytest.raises(StopTest):
        await collector.run()

    # The first ticks still run (no history yet -- is_dark fails open), then
    # once DARK_TICKS dark readings are logged it drops off the watch cadence.
    assert slept[-1] != 300, (
        f"after dark the watch must stand down, got {slept}")
    assert slept[-1] == collector.settings.poll_asleep, (
        f"and fall back to the ordinary asleep cadence: {slept}")

    # --- dawn: the same database, now with sun on the array ---------------
    calls.clear()
    day = _WatchLoopClient(calls, grid_w=-5600.0)

    async def sunny_get(path, ttl=0):
        calls.append("site")
        return {"grid_power": -5600.0, "solar_power": 7000.0}
    day._get = sunny_get
    monkeypatch.setattr(collector, "TeslaClient", lambda settings: day)

    slept.clear()
    with pytest.raises(StopTest):
        await collector.run()

    assert slept[-1] == 300, (
        f"the watch MUST resume once the sun is back, got {slept}")


class _AlreadyChargingClient(_SolarSiteClient):
    """charge_start refused with reason `is_charging` -- the live 2026-07-29
    failure. The car was ALREADY charging, which is success for our purposes,
    not failure."""

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        if name == "charge_start":
            return 200, {"response": {"result": False, "reason": "is_charging"}}
        if name == "set_charging_amps":
            self.amps = params["charging_amps"]
        return 200, {"response": {"result": True}}


@pytest.mark.asyncio
async def test_charge_start_refused_because_it_is_already_charging_is_not_a_failure(
        tmp_path, monkeypatch):
    """Observed live 2026-07-29 10:11, and it cost 23 minutes of sunshine.

    The rollback added for a refused charge_start treats every refusal as
    "the car did not start". But `is_charging` means the opposite: it is
    already running, and the only thing left to do is take control of it.
    Rolling back abandoned a live charge pinned at the 5 A floor while
    2.4 kW was exporting, and -- because "stopped" is not "charging" -- the
    controller then had no reason to look at it again.
    """
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1, restart_hold_s=0)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="stopped", hold_s=0)

    client = _AlreadyChargingClient(house_w=2470.0, solar_w=4890.0)
    client.charging = True          # the car really is charging, at the floor
    client.amps = 5
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    state, _ = await collector.solar_tick(client, store_, "VIN1", client.view(),
                                          cfg, site_id=1)

    assert state == "charging", (
        "a car that refuses charge_start BECAUSE it is charging is charging -- "
        f"rolling back to stopped abandons it at the floor: {client.commands}")
    writes = [c[1]["charging_amps"] for c in client.commands
              if c[0] == "set_charging_amps"]
    assert writes, f"and it must be servoed up to the surplus: {client.commands}"
    assert writes[-1] > 5, (
        f"2.4 kW of surplus should lift it well off the 5 A floor, got {writes}")


@pytest.mark.asyncio
async def test_the_dark_backoff_cannot_blind_itself_to_dawn(tmp_path, monkeypatch):
    """The deadlock the stand-down created, seen live on 2026-07-29.

    solar_ticks only gets a row when solar_tick RUNS. If a tick returns early
    without logging -- as the charge_start rollback did -- the newest rows
    stay yesterday's zeros, is_dark keeps reading them, and the loop holds at
    1800 s straight through a sunny morning. Seeing dawn requires a tick, and
    the back-off had suppressed the tick.

    So darkness must be judged on FRESH readings only. Stale ones mean "we do
    not know", which fails open, exactly like having no history at all.
    """
    now = int(time.time())
    # Three genuinely dark readings, but from last night.
    stale = [{"ts": now - 11 * 3600, "solar_w": 0.0}] * 3
    assert not solar.is_dark_at(stale, now), (
        "readings 11 hours old cannot prove it is dark NOW -- that is how the "
        "loop slept through a sunny morning")

    fresh = [{"ts": now - 300, "solar_w": 0.0},
             {"ts": now - 600, "solar_w": 0.0},
             {"ts": now - 900, "solar_w": 0.0}]
    assert solar.is_dark_at(fresh, now), "recent dark readings still count"


@pytest.mark.asyncio
async def test_already_charging_is_recognised_through_the_proxys_prose():
    """The live string, verbatim from collector.log on 2026-07-29:

        car could not execute command: is_charging

    The signing proxy wraps the car's own reason in a sentence, so an
    equality test against "is_charging" never fires -- which is how the first
    attempt at this fix deployed, changed nothing, and left the car pinned at
    5 A for another half hour. The suite passed both before and after that
    attempt, because nothing in it used the real string.
    """
    class _Wrapped:
        async def command(self, vin, name, params):
            return 200, {"response": {
                "result": False,
                "reason": "car could not execute command: is_charging"}}

    assert await collector._command(_Wrapped(), "VIN1", "charge_start") is True

    # And the pairing still holds through the prose: the same wrapped shape
    # from set_charging_amps is a genuine failure, because the write did not
    # happen and integrating against it would corrupt the control loop.
    class _WrappedAmps:
        async def command(self, vin, name, params):
            return 200, {"response": {
                "result": False,
                "reason": "car could not execute command: is_charging"}}

    assert await collector._command(
        _WrappedAmps(), "VIN1", "set_charging_amps", charging_amps=15) is False


class _NightImportClient:
    """2 kW of import and no sun -- the exact condition that makes the solar
    machine stop a charge. A forced charge must survive it.

    Accepting a set_charging_amps updates the view it was given, because a
    real car does: should_refresh_view() returns True on the tick after any
    write, so the loop always re-reads vehicle_data before deciding again. A
    frozen view would make the controller look like it re-commands forever
    when what it is really doing is waiting for the car to catch up.
    """

    def __init__(self, view=None, grid_w=2000.0):
        self.commands = []
        self.view = view
        self.grid_w = grid_w

    async def _get(self, path, ttl=0):
        return {"grid_power": self.grid_w, "solar_power": 0.0}

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        if name == "set_charging_amps" and self.view is not None:
            self.view["amps_actual"] = params["charging_amps"]
            self.view["charge_amps"] = params["charging_amps"]
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        raise AssertionError("the car was already online")


@pytest.mark.asyncio
async def test_a_forced_charge_survives_ticks_that_solar_would_have_killed(
    tmp_path, monkeypatch,
):
    """THE regression this whole feature exists to prevent.

    With enabled=1 and no force, a night-time tick ADOPTS the running charge
    (solar.py:315-323), writes set_amps against a negative surplus, breaches
    the floor within two ticks, transits grace and issues charge_stop --
    about four billed commands to end exactly where it began.

    With force live, four ticks of the same heavy import must issue no
    charge_stop at all.
    """
    monkeypatch.setattr(tesla, "proxy_up", lambda url: True)

    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1,
                      force_charge_until=int(time.time()) + 3600)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle")

    view = {
        "charging_state": "Charging", "amps_actual": 5, "amps_max": 48,
        "volts": 240, "charge_amps": 24, "soc": 55, "limit": 80,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }
    client = _NightImportClient(view)
    cfg = SimpleNamespace(timezone="America/Denver",
                          proxy_url="https://localhost:4443")

    for _ in range(4):
        await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)

    names = [c[0] for c in client.commands]
    assert "charge_stop" not in names, (
        f"force mode let the solar machine stop the charge: {client.commands}")

    # The car was sitting at the controller's 5 A floor; force must lift it.
    amps = [c[1]["charging_amps"] for c in client.commands
            if c[0] == "set_charging_amps"]
    assert amps and amps[0] == 48, (
        f"expected a lift to amps_max, got {amps}")
    assert len(amps) == 1, (
        f"amps rewritten every tick instead of once: {amps}")

    assert solar.load_state(db, "VIN1")["force_started"] == 1, (
        "the latch never set, so the mode will expire on the next tick")
    store_.close()


class _AsleepLoopClient:
    """A car that never wakes, and a meter showing heavy import. Records
    every wake attempt."""

    def __init__(self, wakes: list[float]):
        self.wakes = wakes

    async def resolve_vin(self):
        return "VIN1"

    async def energy_sites(self):
        return [{"energy_site_id": 1}]

    async def vehicle(self, vin):
        return {"state": "asleep"}

    async def vehicle_data(self, vin, *a, **k):
        raise AssertionError("a sleeping car must not be read")

    async def wake_up(self, vin):
        self.wakes.append(time.time())
        return {"state": "asleep"}      # refuses to come online

    async def _get(self, path, ttl=0):
        return {"grid_power": 2000.0, "solar_power": 0.0}

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_forcing_wakes_a_sleeping_car_at_most_once_per_window(
    monkeypatch, tmp_path,
):
    """Without the wake, "charge my car up" at 2 a.m. fails SILENTLY all
    night: the meter-only watch is disabled while forcing, poll_once returns
    no view for a sleeping car, and the tick gate skips the tick entirely.

    The rate limit is the other half of it. A wake is $0.02 against a
    $10/month credit, and a car that will not wake must not be asked again
    every tick until midnight.
    """
    wakes: list[float] = []
    db_path = tmp_path / "car.db"

    seed = Store(db_path)
    solar.save_config(seed._db, enabled=1,
                      force_charge_until=int(time.time()) + 3600)
    home.save(seed._db, 40.0, -105.0, 100)
    seed.close()

    monkeypatch.setattr(collector.settings, "db_file", db_path)
    monkeypatch.setattr(collector, "TeslaClient",
                        lambda settings: _AsleepLoopClient(wakes))

    sleep_count = 0

    async def fake_sleep(seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 5:
            raise StopTest()
    monkeypatch.setattr(collector.asyncio, "sleep", fake_sleep)

    with pytest.raises(StopTest):
        await collector.run()

    assert len(wakes) == 1, (
        f"5 ticks inside one {collector.FORCE_WAKE_MIN_S}s window issued "
        f"{len(wakes)} wakes; each one is $0.02")


@pytest.mark.asyncio
async def test_a_forced_charge_still_moves_the_attribution_ledger(
    tmp_path, monkeypatch,
):
    """The regression that would silently break "miles added today".

    green.charged_split derives the entire solar/grid split from solar_ticks,
    and a row needs car_w AND grid_w. It is tempting to skip live_status while
    forcing -- there is no control loop to run, so why pay for the meter?
    Because skipping it makes a forced overnight charge INVISIBLE: the ledger
    under-reports by the whole charge and the Energy Dashboard's car meters
    flatline through it. Far worse than the 5-15% downward bias already
    documented in the HA spec.
    """
    monkeypatch.setattr(tesla, "proxy_up", lambda url: True)

    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1,
                      force_charge_until=int(time.time()) + 3600)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle")

    # solar_ticks is keyed (vin, ts) and log_tick does INSERT OR REPLACE, so
    # three ticks inside one wall-clock second would collapse into a single
    # row and this test would read a written ledger as an empty one. Advance
    # a fake clock a second per reading; the force window is an hour wide, so
    # the drift changes nothing else.
    base = time.time()
    ticker = iter(range(1, 10_000))
    monkeypatch.setattr(time, "time", lambda: base + next(ticker))

    view = {
        "charging_state": "Charging", "amps_actual": 48, "amps_max": 48,
        "volts": 240, "charge_amps": 24, "soc": 55, "limit": 80,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }
    # Import must cover the car's own 11.5 kW draw plus the house. The 2 kW
    # the other test uses would be physically impossible beside solar_power=0
    # -- charged_split infers solar as (car_w - grid import), so it would
    # correctly conclude 9.5 kW came from a sun that is not shining.
    client = _NightImportClient(view, grid_w=12000.0)
    cfg = SimpleNamespace(timezone="America/Denver",
                          proxy_url="https://localhost:4443")

    for _ in range(3):
        await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)

    rows = [dict(r) for r in db.execute(
        "SELECT state, car_w, grid_w, period_s FROM solar_ticks WHERE vin = ?",
        ("VIN1",))]
    assert len(rows) == 3, (
        f"force mode logged {len(rows)} ticks, not 3 -- the ledger is blind "
        "to this charge")
    assert all(r["car_w"] is not None and r["grid_w"] is not None
               for r in rows), "a tick row without both watts attributes nothing"

    solar_kwh, grid_kwh = green.charged_split(rows)
    assert grid_kwh > 0, "night-time charging booked no grid energy"
    assert solar_kwh == 0.0, (
        f"attributed {solar_kwh} kWh to the sun at 2 a.m. with solar_power=0")
    store_.close()
