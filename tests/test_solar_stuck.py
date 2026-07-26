"""Spec 5: `grid_power` stuck | same value > 5 consecutive ticks | hold amps,
flag suspect.

The meter refreshes every 60 s against a 120 s loop, so one or two repeats
are normal. A gateway frozen at a large NEGATIVE (export) reading is the
dangerous case: error_w stays positive forever, the controller ramps to
max_a and holds there, and nothing in the state machine can ever observe the
floor breach that would stop it -- pulling full grid power all night while
the meter insists it is sunny. The only backstop today is the car's own
charge limit.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

import collector
import home
import solar
from store import Store


def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(solar.SCHEMA)
    return conn


# --------------------------------------------------------------------------
# grid_is_stuck -- pure function table.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,recent,expect", [
    ("fewer than threshold, even all identical",       [-500.0] * 5, False),
    ("far shorter than threshold",                      [-500.0] * 2, False),
    ("empty history",                                   [], False),
    ("exactly threshold, all identical -> stuck",       [-500.0] * 6, True),
    ("exactly threshold, one tick differs -> not stuck", [-500.0] * 5 + [-499.0], False),
    ("more than threshold, all identical -> stuck",     [-500.0] * 9, True),
    ("alternating values never look stuck",             [-500.0, -600.0] * 3, False),
])
def test_grid_is_stuck_table(name, recent, expect):
    assert solar.grid_is_stuck(recent) is expect, name


def test_default_threshold_is_six_matching_spec_5():
    """Spec 5 says '> 5 consecutive ticks' -- six is the first count that
    satisfies that, not five."""
    assert solar.grid_is_stuck([1.0] * 5) is False
    assert solar.grid_is_stuck([1.0] * 6) is True


def test_a_custom_threshold_is_honoured():
    assert solar.grid_is_stuck([1.0, 1.0, 1.0], threshold=3) is True
    assert solar.grid_is_stuck([1.0, 1.0], threshold=3) is False


# --------------------------------------------------------------------------
# recent_grid_w -- the history query grid_is_stuck is fed from.
# --------------------------------------------------------------------------

def test_recent_grid_w_is_newest_first_and_respects_limit():
    conn = db()
    for i, w in enumerate([100.0, 200.0, 300.0, 400.0]):
        solar.log_tick(conn, "V1", ts=1000 + i, state="charging", grid_w=w, period_s=120)
    assert solar.recent_grid_w(conn, "V1", 2) == [400.0, 300.0]
    assert solar.recent_grid_w(conn, "V1", 10) == [400.0, 300.0, 200.0, 100.0]


def test_recent_grid_w_is_empty_before_any_tick_is_logged():
    conn = db()
    assert solar.recent_grid_w(conn, "V1", 6) == []


def test_recent_grid_w_is_scoped_to_the_requested_vin():
    conn = db()
    solar.log_tick(conn, "V1", ts=1000, state="charging", grid_w=111.0, period_s=120)
    solar.log_tick(conn, "V2", ts=1001, state="charging", grid_w=222.0, period_s=120)
    assert solar.recent_grid_w(conn, "V1", 10) == [111.0]


# --------------------------------------------------------------------------
# Integration: collector.solar_tick, driven for real, against a fake client
# whose grid_power never changes -- the frozen-gateway scenario itself.
# --------------------------------------------------------------------------

class _FrozenMeterClient:
    """Returns the SAME grid_power on every call, forever. solar_power is
    left large and positive so a decoder that accidentally keyed off it
    instead would not mask the freeze."""

    def __init__(self, grid_w: float):
        self.grid_w = grid_w
        self.commands: list[tuple[str, dict]] = []

    async def _get(self, path, ttl=0):
        return {"grid_power": self.grid_w, "solar_power": 9000.0}

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        raise AssertionError("must never wake while grid_power is stuck")


@pytest.mark.asyncio
async def test_stuck_grid_power_holds_amps_once_the_run_length_is_exceeded(tmp_path, monkeypatch):
    """A car already charging, comfortably below max_a. A frozen -6000 W
    export reading would otherwise ramp it straight to max_a and hold it
    there -- no floor breach, no grace, no stop, all night.

    The first GRID_STUCK_TICKS - 1 ticks are the honest 60s/120s refresh
    overlap the spec calls out as normal, so they may still command. Once the
    run length exceeds the threshold, no further set_charging_amps may be
    issued, and a tick row must still be logged so the freeze is visible in
    the record.
    """
    # solar_ticks' primary key is (vin, ts): a test that fires many ticks
    # within the same wall-clock second would have each INSERT OR REPLACE
    # collapse onto the SAME row, silently erasing the very history
    # recent_grid_w() and grid_is_stuck() depend on. Advance a fake clock a
    # full period per tick so every logged row is distinct, the way it would
    # be for a real 120 s loop.
    fake_now = [1_700_000_000]

    def fake_time():
        fake_now[0] += 130
        return fake_now[0]
    monkeypatch.setattr(collector.time, "time", fake_time)

    store_ = Store(tmp_path / "car.db")
    db_ = store_._db
    solar.save_config(db_, enabled=1)
    home.save(db_, 40.0, -105.0, 100)
    solar.save_state(db_, "VIN1", state="charging", dirty=1, original_amps=5,
                     engaged_at=1)

    client = _FrozenMeterClient(grid_w=-6000.0)
    cfg = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")
    view = {
        "charging_state": "Charging", "amps_actual": 10, "amps_max": 48,
        "volts": 240, "charge_amps": 10, "soc": 50, "limit": 80,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }

    warmup = solar.GRID_STUCK_TICKS - 1   # normal repetition -- not yet "stuck"
    for _ in range(warmup):
        await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)

    commands_before = len(client.commands)
    assert commands_before > 0, "premise: the ramp must be actively commanding pre-freeze"

    for _ in range(4):
        state, wrote = await collector.solar_tick(client, store_, "VIN1", view, cfg, site_id=1)
        assert state == "charging"
        assert wrote is False, "a detected-stuck tick must never report a write"

    assert len(client.commands) == commands_before, (
        f"a command was issued after the meter was detected stuck: "
        f"{client.commands[commands_before:]}")

    rows = db_.execute(
        "SELECT note FROM solar_ticks WHERE vin = ? AND note = 'grid_power stuck'",
        ("VIN1",),
    ).fetchall()
    assert len(rows) >= 1, "a tick row must still be logged while the meter is frozen"
    store_.close()
