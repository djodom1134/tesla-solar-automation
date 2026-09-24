from __future__ import annotations

import time

import pytest

import ha_routes
import home
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


@pytest.mark.asyncio
async def test_battery_out_is_pinned_at_zero_because_there_is_no_v2h(tmp_path, monkeypatch):
    """The car is storage, but one-way storage.

    HA computes home = solar + grid_in - grid_out + battery_out - battery_in.
    Charging the car really is not house consumption, so battery_in belongs.
    But this site has no vehicle-to-home: reporting driving energy as
    battery_out would ADD it to home consumption and inflate the house load by
    every mile driven -- energy that physically left the property.
    """
    st = Store(tmp_path / "car.db")
    solar.save_config(st._db, enabled=1)
    st.record({"vin": "VIN1", "soc": 60, "sampled_at": int(time.time()),
               "charging_state": "Stopped", "amps_actual": 0, "charging": 0},
              at_home=True)
    solar.log_tick(st._db, "VIN1", ts=int(time.time()), state="charging",
                   grid_w=-1000.0, solar_w=5000.0, car_w=4000.0, period_s=3600)
    monkeypatch.setattr(ha_routes, "DEMO", False)
    monkeypatch.setattr(ha_routes, "store", lambda: st)
    monkeypatch.setattr(ha_routes.settings, "vin", "VIN1")

    body = await ha_routes.ha_meters()
    assert body["battery_in_kwh"] == body["car_total_kwh"], (
        "everything charged into the car is energy stored, not consumed")
    assert body["battery_out_kwh"] == 0.0, (
        "no V2H -- the car never gives energy back to the house")
    st.close()


@pytest.mark.asyncio
async def test_live_power_comes_from_the_tick_log_not_a_billed_call(tmp_path, monkeypatch):
    """live_status is billed. The collector already pays for one every tick
    and writes the result, so serving it again is free -- at the cost of
    freshness, which the HA templates gate on rather than hiding."""
    st = Store(tmp_path / "car.db")
    solar.save_config(st._db, enabled=1)
    solar.save_state(st._db, "VIN1", heartbeat_ts=int(time.time()),
                     heartbeat_sleep_s=120)
    st.record({"vin": "VIN1", "soc": 60, "sampled_at": int(time.time()),
               "charging_state": "Charging", "amps_actual": 16, "charging": 1},
              at_home=True)
    solar.log_tick(st._db, "VIN1", ts=int(time.time()), state="charging",
                   grid_w=-1500.0, solar_w=6000.0, car_w=3840.0, period_s=120)
    monkeypatch.setattr(ha_routes, "DEMO", False)
    monkeypatch.setattr(ha_routes, "store", lambda: st)
    monkeypatch.setattr(ha_routes.settings, "vin", "VIN1")

    body = await ha_routes.ha_state()
    assert body["solar_w"] == 6000.0
    assert body["grid_w"] == -1500.0
    assert body["grid_export_w"] == 1500.0, "exporting splits out non-negative"
    assert body["grid_import_w"] == 0.0
    # house = site consumption minus the car: 6000 + (-1500) - 3840
    assert body["house_w"] == pytest.approx(660.0)
    # And the whole vehicle view rides along, so a new Tesla field needs a
    # template rather than an endpoint change.
    assert body["car"]["soc"] == 60
    st.close()


# --------------------------------------------------------------------------
# should_plug_in: sunshine going to the grid for want of a cable. Nothing
# here can plug a car in, so the endpoint reports it and a Home Assistant
# automation does the notifying.
# --------------------------------------------------------------------------

def _plug_in_store(tmp_path, *, soc, charging_state, lat=40.0, lon=-105.0,
                   surplus_w=4000.0):
    st = Store(tmp_path / "car.db")
    solar.save_config(st._db, enabled=1, soc_ceiling=95)
    home.save(st._db, 40.0, -105.0, 100)
    solar.save_state(st._db, "VIN1", heartbeat_ts=int(time.time()),
                     heartbeat_sleep_s=120)
    st.record({"vin": "VIN1", "soc": soc, "limit": 80,
               "sampled_at": int(time.time()), "charging_state": charging_state,
               "amps_actual": 0, "charging": 0, "lat": lat, "lon": lon},
              at_home=True)
    solar.log_tick(st._db, "VIN1", ts=int(time.time()), state="idle",
                   grid_w=-surplus_w, solar_w=surplus_w, car_w=0.0,
                   surplus_w=surplus_w, error_w=0.0, amps_before=0,
                   amps_target=0, amps_written=None, soc=soc, import_w=0.0,
                   period_s=120)
    return st


async def _plug_in_body(tmp_path, monkeypatch, **kw):
    st = _plug_in_store(tmp_path, **kw)
    monkeypatch.setattr(ha_routes, "DEMO", False)
    monkeypatch.setattr(ha_routes, "store", lambda: st)
    monkeypatch.setattr(ha_routes.settings, "vin", "VIN1")
    try:
        return await ha_routes.ha_state()
    finally:
        st.close()


@pytest.mark.asyncio
async def test_state_flags_an_unplugged_car_wasting_sunshine(tmp_path, monkeypatch):
    body = await _plug_in_body(tmp_path, monkeypatch, soc=60,
                               charging_state="Disconnected")
    assert body["should_plug_in"] is True


@pytest.mark.asyncio
async def test_state_does_not_nag_about_a_car_already_plugged_in(tmp_path, monkeypatch):
    body = await _plug_in_body(tmp_path, monkeypatch, soc=60,
                               charging_state="Stopped")
    assert body["plugged_in"] is True
    assert body["should_plug_in"] is False


@pytest.mark.asyncio
async def test_state_does_not_nag_about_a_car_that_is_elsewhere(tmp_path, monkeypatch):
    body = await _plug_in_body(tmp_path, monkeypatch, soc=60,
                               charging_state="Disconnected",
                               lat=41.0, lon=-106.0)
    assert body["location"] == "away"
    assert body["should_plug_in"] is False


@pytest.mark.asyncio
async def test_state_does_not_nag_when_there_is_no_sun(tmp_path, monkeypatch):
    body = await _plug_in_body(tmp_path, monkeypatch, soc=60,
                               charging_state="Disconnected", surplus_w=200.0)
    assert body["should_plug_in"] is False


@pytest.mark.asyncio
async def test_state_does_not_nag_about_a_car_at_the_ceiling(tmp_path, monkeypatch):
    body = await _plug_in_body(tmp_path, monkeypatch, soc=96,
                               charging_state="Disconnected")
    assert body["should_plug_in"] is False


# --------------------------------------------------------------------------
# The blind controller -- see solar.controller_blind and the day of
# 2026-09-14 that it exists for.
# --------------------------------------------------------------------------

def _blind_store(tmp_path, *, tick_age_s, soc=84, limit=99):
    st = Store(tmp_path / "car.db")
    solar.save_config(st._db, enabled=1)
    home.save(st._db, 40.0, -105.0, 100)
    # Alive and beating on its asleep cadence -- which is exactly how the
    # real thing looked all day while it saw nothing.
    solar.save_state(st._db, "VIN1", heartbeat_ts=int(time.time()),
                     heartbeat_sleep_s=1800)
    st.record({"vin": "VIN1", "soc": soc, "limit": limit,
               "sampled_at": int(time.time()) - tick_age_s,
               "charging_state": "Stopped", "amps_actual": 0, "charging": 0,
               "lat": 40.0, "lon": -105.0}, at_home=True)
    solar.log_tick(st._db, "VIN1", ts=int(time.time()) - tick_age_s,
                   state="stopped", grid_w=-4000.0, solar_w=4000.0, car_w=0.0,
                   surplus_w=4000.0, error_w=0.0, amps_before=0, amps_target=0,
                   amps_written=None, soc=soc, import_w=0.0, period_s=1800)
    return st


async def _blind_body(tmp_path, monkeypatch, **kw):
    st = _blind_store(tmp_path, **kw)
    monkeypatch.setattr(ha_routes, "DEMO", False)
    monkeypatch.setattr(ha_routes, "store", lambda: st)
    monkeypatch.setattr(ha_routes.settings, "vin", "VIN1")
    try:
        return await ha_routes.ha_state()
    finally:
        st.close()


@pytest.mark.asyncio
async def test_state_reports_a_controller_that_has_stopped_looking(
        tmp_path, monkeypatch):
    """Seventeen hours without a tick, against a live heartbeat. This is the
    signal that was missing on 2026-09-14."""
    body = await _blind_body(tmp_path, monkeypatch, tick_age_s=17 * 3600)
    assert body["collector_running"] is True, (
        "the process really was alive -- that is the whole problem")
    assert body["controller_blind"] is True


@pytest.mark.asyncio
async def test_state_does_not_cry_blind_over_an_ordinary_asleep_cadence(
        tmp_path, monkeypatch):
    """One tick per 1800 s is a healthy night, not a blind one."""
    body = await _blind_body(tmp_path, monkeypatch, tick_age_s=1800)
    assert body["controller_blind"] is False


@pytest.mark.asyncio
async def test_state_does_not_cry_blind_over_a_car_with_no_headroom(
        tmp_path, monkeypatch):
    """A car at its limit is one the watch refuses on purpose."""
    body = await _blind_body(tmp_path, monkeypatch, tick_age_s=17 * 3600,
                             soc=99, limit=99)
    assert body["controller_blind"] is False


@pytest.mark.asyncio
async def test_state_reports_sun_going_to_waste_on_a_car_it_could_fill(
        tmp_path, monkeypatch):
    """2026-09-19: exporting, plugged in at home, a car with headroom under
    the ceiling, nothing charging -- and every existing flag quiet.
    controller_blind asks whether ticks happen; this asks whether they are
    achieving anything. HA owns the notifying."""
    body = await _blind_body(tmp_path, monkeypatch, tick_age_s=1200)
    assert body["sun_wasted"] is True
    assert body["schema"] == 1, "an additive field must not bump the schema"


@pytest.mark.asyncio
async def test_state_does_not_cry_waste_over_a_car_at_the_ceiling(
        tmp_path, monkeypatch):
    """At the ceiling the raise is not allowed to help, so there is nothing
    to report and an alarm would be noise."""
    body = await _blind_body(tmp_path, monkeypatch, tick_age_s=1200,
                             soc=95, limit=95)
    assert body["sun_wasted"] is False


@pytest.mark.asyncio
async def test_state_goes_quiet_once_the_evidence_is_last_afternoons(
        tmp_path, monkeypatch):
    """The collector stops ticking at sundown, so the last exporting row of
    the day sits in the log all night. HA announces these out loud; a flag
    still true at 2 a.m. is an announcement about this afternoon."""
    body = await _blind_body(tmp_path, monkeypatch, tick_age_s=6 * 3600)
    assert body["sun_wasted"] is False
    assert body["should_plug_in"] is False


@pytest.mark.asyncio
async def test_nothing_announces_after_sundown(tmp_path, monkeypatch):
    """2026-09-24, the third night running. A stalled loop leaves exactly the
    conditions every alarm here fires on -- no ticks, an aging view -- and
    the owner heard about it at 2 a.m. Sundown silences all of them, computed
    from the site's coordinates so a silent collector cannot unsilence it."""
    import time as _time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    two_am = datetime(2026, 9, 24, 2, tzinfo=ZoneInfo("America/Denver")).timestamp()
    monkeypatch.setattr(_time, "time", lambda: two_am)
    # A day-old view, no tick for seventeen hours: blind AND stale AND, but
    # for the hour, worth saying so.
    body = await _blind_body(tmp_path, monkeypatch, tick_age_s=17 * 3600)
    assert body["sun_up"] is False
    for flag in ("controller_blind", "knowledge_stale", "sun_wasted",
                 "should_plug_in"):
        assert body[flag] is False, f"{flag} spoke at 2 a.m."


@pytest.mark.asyncio
async def test_the_same_state_does_announce_in_daylight(tmp_path, monkeypatch):
    """The discriminating half: quiet hours must not become permanent quiet.
    The same stalled loop at ten in the morning is news."""
    import time as _time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ten_am = datetime(2026, 9, 24, 10, tzinfo=ZoneInfo("America/Denver")).timestamp()
    monkeypatch.setattr(_time, "time", lambda: ten_am)
    body = await _blind_body(tmp_path, monkeypatch, tick_age_s=17 * 3600)
    assert body["sun_up"] is True
    assert body["controller_blind"] is True
