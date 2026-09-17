"""The manual-override pause, driven through the REAL collector.solar_tick.

test_override_pause.py covers the detection rule as arithmetic. This file
covers the thing that actually matters to the owner: after they move the
charge-rate slider in the Tesla app, does the controller stop writing over
them -- and does it stop without undoing the rate they just chose?
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import collector
import home
import solar
import tesla
from store import Store

CFG = SimpleNamespace(timezone="America/Denver", proxy_url="https://localhost:4443")


class _ExportClient:
    """6 kW of steady solar export -- the healthy engaged state, so nothing
    the controller does here can be blamed on the weather."""

    def __init__(self):
        self.commands = []

    async def _get(self, path, ttl=0):
        return {"grid_power": -6000.0, "solar_power": 6000.0}

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        raise AssertionError("must never wake a car that never slept")


def _view(**over):
    view = {
        "charging_state": "Charging", "amps_actual": 24, "amps_max": 48,
        "volts": 240, "charge_amps": 24, "soc": 55, "limit": 80,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }
    view.update(over)
    return view


def _engaged(tmp_path, monkeypatch, **conf):
    """A store whose controller is already mid-engagement at 24 A, with the
    owner's own 48 A and 80% recorded for restore -- the state an override
    actually happens in."""
    monkeypatch.setattr(tesla, "proxy_up", lambda url: True)
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1, **conf)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(
        db, "VIN1", state="charging", dirty=1, original_amps=48,
        original_limit=80, commanded_amps=24, commanded_ack=1)
    return store_, db


# --------------------------------------------------------------------------
# The latch
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_owners_rate_is_left_exactly_where_they_put_it(tmp_path, monkeypatch):
    """The whole point. The owner sets 32 A; the controller must not write
    ANY amps value on that tick -- not its own solar-derived target, and not
    the 48 A it had recorded to restore. Restoring here would be the same
    defeat as overwriting, just wearing a more helpful face."""
    store_, db = _engaged(tmp_path, monkeypatch)
    client = _ExportClient()

    await collector.solar_tick(client, store_, "VIN1", _view(charge_amps=32),
                               CFG, site_id=1)

    amps = [c for c in client.commands if c[0] == "set_charging_amps"]
    assert amps == [], f"wrote amps on the override tick: {client.commands}"
    st = solar.load_state(db, "VIN1")
    assert st["override_amps"] == 32
    assert st["override_since"]
    store_.close()


@pytest.mark.asyncio
async def test_a_raised_charge_limit_is_put_back_on_the_way_out(tmp_path, monkeypatch):
    """Undoing OUR change is not fighting the owner; leaving it raised is.
    A car left at a 90% limit would keep charging past the 80% they set, at
    the rate they just chose."""
    store_, db = _engaged(tmp_path, monkeypatch)
    solar.save_state(db, "VIN1", raised_to=90)
    client = _ExportClient()

    await collector.solar_tick(client, store_, "VIN1",
                               _view(charge_amps=32, limit=90), CFG, site_id=1)

    limits = [c for c in client.commands if c[0] == "set_charge_limit"]
    assert limits == [("set_charge_limit", {"percent": 80})], client.commands
    st = solar.load_state(db, "VIN1")
    assert st["dirty"] == 0
    assert st["original_amps"] is None
    assert st["raised_to"] is None
    store_.close()


@pytest.mark.asyncio
async def test_a_limit_we_never_raised_is_not_touched(tmp_path, monkeypatch):
    """raised_to is NULL: the 80% on the car is the owner's own. Writing it
    back would spend a billed command to re-assert a value nothing changed."""
    store_, db = _engaged(tmp_path, monkeypatch)
    client = _ExportClient()

    await collector.solar_tick(client, store_, "VIN1", _view(charge_amps=32),
                               CFG, site_id=1)

    assert [c for c in client.commands if c[0] == "set_charge_limit"] == []
    store_.close()


@pytest.mark.asyncio
async def test_once_paused_the_controller_says_nothing_at_all(tmp_path, monkeypatch):
    """Five further ticks of glorious export. The surplus is real and the
    controller can see it -- and must still keep its hands off."""
    store_, db = _engaged(tmp_path, monkeypatch)
    client = _ExportClient()
    view = _view(charge_amps=32, amps_actual=32)

    await collector.solar_tick(client, store_, "VIN1", view, CFG, site_id=1)
    after_latch = len(client.commands)
    for _ in range(5):
        await collector.solar_tick(client, store_, "VIN1", view, CFG, site_id=1)

    assert len(client.commands) == after_latch, (
        f"commanded the car while paused: {client.commands[after_latch:]}")
    assert solar.load_state(db, "VIN1")["state"] == "idle"
    store_.close()


@pytest.mark.asyncio
async def test_the_pause_still_logs_ticks(tmp_path, monkeypatch):
    """A manual charge that went unlogged would be invisible to
    green.charged_split and so to the banked-solar ledger -- the car would
    quietly fill with grid electrons the tank never accounted for. Same
    reasoning that keeps force-mode ticks logged."""
    store_, db = _engaged(tmp_path, monkeypatch)
    client = _ExportClient()
    view = _view(charge_amps=32, amps_actual=32)

    # solar_ticks is keyed on (vin, ts), so three ticks inside one second
    # collapse to a single row and the count below would prove nothing about
    # what was logged. Advance a fake clock a period at a time instead.
    clock = [1_800_000_000.0]
    monkeypatch.setattr(collector.time, "time", lambda: clock[0])
    for _ in range(3):
        await collector.solar_tick(client, store_, "VIN1", view, CFG, site_id=1)
        clock[0] += 120

    rows = db.execute(
        "SELECT state, car_w FROM solar_ticks WHERE vin = ? ORDER BY ts",
        ("VIN1",)).fetchall()
    assert len(rows) == 3
    assert [r["state"] for r in rows] == ["idle", "idle", "idle"]
    assert all(r["car_w"] > 0 for r in rows), (
        "the car's own draw must be logged, or the ledger cannot attribute it")
    store_.close()


# --------------------------------------------------------------------------
# Not an override
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_write_the_car_has_not_reported_yet_does_not_pause(tmp_path, monkeypatch):
    """The false positive that would make this feature worse than useless.
    The controller has just written 24 A and the car is still reporting the
    48 A it held a moment ago -- ordinary propagation lag, not a decision."""
    store_, db = _engaged(tmp_path, monkeypatch)
    solar.save_state(db, "VIN1", commanded_amps=24, commanded_ack=0)
    client = _ExportClient()

    await collector.solar_tick(client, store_, "VIN1", _view(charge_amps=48),
                               CFG, site_id=1)

    assert solar.load_state(db, "VIN1")["override_amps"] is None
    store_.close()


@pytest.mark.asyncio
async def test_the_setting_switched_off_restores_the_old_behaviour(tmp_path, monkeypatch):
    """pause_on_override=0: the controller wins, exactly as it did before
    this feature existed."""
    store_, db = _engaged(tmp_path, monkeypatch, pause_on_override=0)
    client = _ExportClient()

    await collector.solar_tick(client, store_, "VIN1", _view(charge_amps=32),
                               CFG, site_id=1)

    st = solar.load_state(db, "VIN1")
    assert st["override_amps"] is None
    assert st["state"] == "charging"
    store_.close()


# --------------------------------------------------------------------------
# Resuming: stopped, then started again
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_charging_on_through_the_pause_does_not_resume(tmp_path, monkeypatch):
    """The car was charging when the owner took it over, and goes on
    charging. One "Charging" reading is not a restart."""
    store_, db = _engaged(tmp_path, monkeypatch)
    client = _ExportClient()
    view = _view(charge_amps=32, amps_actual=32)

    for _ in range(3):
        await collector.solar_tick(client, store_, "VIN1", view, CFG, site_id=1)

    assert solar.load_state(db, "VIN1")["override_amps"] == 32
    store_.close()


@pytest.mark.asyncio
async def test_stopping_then_starting_hands_control_back(tmp_path, monkeypatch):
    """The release condition the owner was promised: stop charging, start it
    again, and the controller is driving once more."""
    store_, db = _engaged(tmp_path, monkeypatch)
    client = _ExportClient()

    await collector.solar_tick(client, store_, "VIN1",
                               _view(charge_amps=32, amps_actual=32), CFG, site_id=1)
    assert solar.load_state(db, "VIN1")["override_amps"] == 32

    await collector.solar_tick(client, store_, "VIN1",
                               _view(charging_state="Stopped", charge_amps=32,
                                     amps_actual=0), CFG, site_id=1)
    assert solar.load_state(db, "VIN1")["override_armed"] == 1

    await collector.solar_tick(client, store_, "VIN1",
                               _view(charge_amps=32, amps_actual=32), CFG, site_id=1)
    st = solar.load_state(db, "VIN1")
    assert st["override_amps"] is None
    assert st["override_armed"] == 0
    assert st["state"] == "charging", "the controller should be driving again"
    # The resume clears the whole handshake, not just the latch. What is in
    # commanded_amps now is this tick's own re-engagement write, and it is
    # unacknowledged -- a stale 24 A carried through from before the override
    # would let the very next tick read the owner's rate as a fresh override
    # and latch all over again.
    assert st["commanded_ack"] == 0
    assert st["commanded_amps"] != 24
    store_.close()


@pytest.mark.asyncio
async def test_unplugging_and_plugging_back_in_hands_control_back(tmp_path, monkeypatch):
    store_, db = _engaged(tmp_path, monkeypatch)
    client = _ExportClient()

    await collector.solar_tick(client, store_, "VIN1",
                               _view(charge_amps=32, amps_actual=32), CFG, site_id=1)
    await collector.solar_tick(client, store_, "VIN1",
                               _view(charging_state="Disconnected", charge_amps=32,
                                     amps_actual=0), CFG, site_id=1)
    await collector.solar_tick(client, store_, "VIN1",
                               _view(charge_amps=32, amps_actual=32), CFG, site_id=1)

    assert solar.load_state(db, "VIN1")["override_amps"] is None
    store_.close()


# --------------------------------------------------------------------------
# The handshake, as the collector actually maintains it
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_controllers_own_write_is_recorded_and_not_mistaken_for_the_owner(
    tmp_path, monkeypatch,
):
    """An ordinary engaged tick writes a solar-derived rate. That value must
    land in commanded_amps with the acknowledgement reset, or the very next
    tick reads the car's not-yet-updated rate as an override and pauses the
    feature on its own command."""
    monkeypatch.setattr(tesla, "proxy_up", lambda url: True)
    store_ = Store(tmp_path / "car.db")
    db = store_._db
    solar.save_config(db, enabled=1)
    home.save(db, 40.0, -105.0, 100)
    solar.save_state(db, "VIN1", state="idle", hold_s=10_000)
    client = _ExportClient()
    view = _view(charging_state="Stopped", amps_actual=0, charge_amps=24)

    await collector.solar_tick(client, store_, "VIN1", view, CFG, site_id=1)

    st = solar.load_state(db, "VIN1")
    written = [c for c in client.commands if c[0] == "set_charging_amps"]
    assert written, f"expected an engagement write: {client.commands}"
    assert st["commanded_amps"] == written[-1][1]["charging_amps"]
    assert st["commanded_ack"] == 0
    assert st["override_amps"] is None
    store_.close()
