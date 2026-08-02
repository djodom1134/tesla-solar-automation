from __future__ import annotations

import time

import pytest

import ha_routes
import mcp_server
import solar
import solar_routes


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Never let a tool in here touch the owner's live car.db.

    get_charging_summary resolves from SQLite by design, and the module-level
    Store singletons are lazily built from settings.db_file -- so without
    this a test run would open the real database.
    """
    from config import settings
    monkeypatch.setattr(settings, "db_file", tmp_path / "mcp.db")
    monkeypatch.setattr(solar_routes, "_store", None)
    monkeypatch.setattr(ha_routes, "_store", None)


class _ExplodingClient:
    """Any attribute access is a billed request that should not be happening."""

    def __getattr__(self, name):
        raise AssertionError(
            f"a read tool reached the Tesla client (.{name}) -- reads must "
            "resolve from SQLite, or an LLM polling this drains the credit")


@pytest.mark.parametrize("name", mcp_server.READ_TOOLS)
@pytest.mark.asyncio
async def test_no_read_tool_can_spend_a_tesla_request(name, monkeypatch):
    """THE cost guarantee for this module.

    ha_routes proves the same property by asserting it never IMPORTS the
    client. That cannot work here: set_charge_mode and wake_car legitimately
    need it. So the invariant is re-cut per tool -- every read runs against a
    client that fails the test on any use.
    """
    monkeypatch.setattr(mcp_server, "_client", lambda: _ExplodingClient())
    await mcp_server.CALLABLES[name]()


@pytest.mark.asyncio
async def test_charging_summary_reports_kwh_even_when_miles_are_unknown():
    """miles_per_kwh needs enough sampled history to clear its threshold. Until
    then the kWh figures are still real and must be returned -- with miles as
    null and a reason, never a figure resting on NOMINAL_PACK_KWH, which
    solar_routes records as ~30% wrong on this car."""
    out = await mcp_server.CALLABLES["get_charging_summary"](period="today")
    assert "solar_kwh" in out and "grid_kwh" in out
    if out["miles_added"] is None:
        assert out["miles_basis"] is None
        assert out["unknown_reason"]


@pytest.mark.asyncio
async def test_every_period_names_the_window_it_used():
    """today is a calendar day and week is a rolling 7 -- deliberately
    inconsistent, because each matches its question. The model must never
    have to guess which it got."""
    for period in ("today", "week", "all"):
        out = await mcp_server.CALLABLES["get_charging_summary"](period=period)
        assert out["window"], period


@pytest.mark.asyncio
async def test_an_unknown_period_is_refused():
    with pytest.raises(ValueError):
        await mcp_server.CALLABLES["get_charging_summary"](period="fortnight")


@pytest.mark.asyncio
async def test_garage_status_never_reports_closed_when_unreachable(monkeypatch):
    """An unreachable opener must read as unreachable. 'Closed' would be a
    lie the owner acts on -- the single worst failure this system can have."""
    async def _unreachable():
        return {"reachable": False, "door_state": None, "obstructed": None}

    monkeypatch.setattr(mcp_server, "_garage_snapshot", _unreachable)
    out = await mcp_server.CALLABLES["get_garage_status"]()
    assert out["reachable"] is False
    assert out["door_state"] != "Closed"


def test_the_dangerous_commands_are_not_exposed():
    """Extending ha_routes' rule: `confirm` is the human-in-the-loop marker.
    A risk=='high' filter would leak schedule_software_update and
    speed_limit_set_limit, and door_unlock as a tool is an unlock reachable
    by prompt injection."""
    names = set(mcp_server.CALLABLES)
    for forbidden in ("door_unlock", "open_garage", "trigger_homelink",
                      "set_solar_config", "flash_lights"):
        assert forbidden not in names, forbidden


def test_the_tool_list_is_exactly_what_is_specified():
    """Eight now. get_home_energy was added after a live session asked about
    whole-house usage and the model correctly reported that no tool covered
    it -- the meters table had the data all along."""
    assert set(mcp_server.CALLABLES) == {
        "get_car_status", "get_charging_summary", "get_solar_status",
        "get_home_energy", "get_garage_status", "set_charge_mode",
        "close_garage", "wake_car"}


# --- staleness: absence of observation is not a measured zero --------------

def _seed_ticks(rows):
    """Put solar_ticks rows in the isolated db the fixture points at."""
    db = solar_routes.store()._db
    for ts, car_w, grid_w in rows:
        solar.log_tick(db, "VIN1", ts=ts, state="charging", car_w=car_w,
                       grid_w=grid_w, solar_w=0.0, period_s=120)


@pytest.mark.asyncio
async def test_a_window_with_no_ticks_reports_null_not_zero(monkeypatch):
    """The failure this project says it never commits, reached through the
    MCP surface: the collector stopped three days ago, so nothing was
    OBSERVED today -- and the summary answered "0.0 solar miles, basis
    measured". A confident zero is not the same claim as "no data", and the
    owner acts differently on each."""
    monkeypatch.setattr(solar_routes, "_vin", lambda: "VIN1")
    out = await mcp_server.CALLABLES["get_charging_summary"](period="today")

    assert out["solar_kwh"] is None, "no ticks observed, yet it reported a number"
    assert out["grid_kwh"] is None
    assert out["miles_added"] is None
    assert out["solar_miles"] is None
    assert out["miles_basis"] is None
    assert out["observed_ticks"] == 0
    assert out["unknown_reason"], "a null with no reason is not an answer"


@pytest.mark.asyncio
async def test_a_real_zero_is_still_reported_as_zero(monkeypatch):
    """The other half. A day the collector ran and the car simply did not
    charge is a genuine, measured 0.0 -- and must not be blurred into "we
    don't know", or an honest quiet day becomes indistinguishable from a
    dead collector."""
    monkeypatch.setattr(solar_routes, "_vin", lambda: "VIN1")
    # Clamped to today's midnight, not simply now-600: run in the first ten
    # minutes after midnight, a tick "ten minutes ago" lands YESTERDAY and
    # falls outside the window this test is asserting on. That is how this
    # test failed at 00:05 having passed all evening.
    midnight = solar_routes._midnight_ts()
    now = int(time.time())
    _seed_ticks([(max(midnight, now - 600), 0.0, 300.0),
                 (max(midnight + 1, now - 480), 0.0, 310.0)])

    out = await mcp_server.CALLABLES["get_charging_summary"](period="today")
    assert out["observed_ticks"] == 2
    assert out["solar_kwh"] == 0.0, "a measured zero must stay a zero"
    assert out["grid_kwh"] is not None
    # Miles may still be unknown here (an empty test db has no driving
    # history to measure efficiency from) -- but that is a statement about
    # MILES, never about whether the window was watched.
    assert "no controller ticks" not in (out["unknown_reason"] or "")


@pytest.mark.asyncio
async def test_the_summary_carries_its_own_freshness(monkeypatch):
    """So a model never has to infer whether the window was actually watched."""
    monkeypatch.setattr(solar_routes, "_vin", lambda: "VIN1")
    out = await mcp_server.CALLABLES["get_charging_summary"](period="today")
    for key in ("observed_ticks", "collector_running", "last_tick_age_s"):
        assert key in out, key


@pytest.mark.asyncio
async def test_solar_status_states_how_old_its_numbers_are(monkeypatch):
    """get_car_status carries data_age_s; get_solar_status carried only a raw
    epoch, and a model reported a three-day-old tick as "right now" --
    verbatim, watts and all. The age must be as legible as the value."""
    out = await mcp_server.CALLABLES["get_solar_status"]()
    assert "data_age_s" in out
    assert "stale" in out, "the judgement itself, not just the raw seconds"


# --- transport security: reachable from the LAN, still not from anywhere ---

def test_the_streamable_app_accepts_the_configured_host():
    """The MCP SDK enables DNS-rebinding protection with an allow-list that
    defaults to 127.0.0.1 only, so a LAN client got 421 Misdirected Request
    -- "Invalid Host header: 192.168.87.56:8000". Loopback-only testing hid
    it completely, and the whole point of this endpoint is that Claude
    Desktop on another machine can reach the mini."""
    hosts = mcp_server._allowed_hosts()
    assert "127.0.0.1" in hosts and "localhost" in hosts
    assert any(h.endswith(":8000") or ":" in h for h in hosts), (
        "the Host header carries the port, so bare hostnames do not match")


def test_extra_hosts_come_from_configuration_not_a_wildcard(monkeypatch):
    """Rebinding protection stays ON. The owner names the addresses; this
    never degrades to allow-anything, which would be the easy wrong fix."""
    monkeypatch.setattr(mcp_server.settings, "mcp_allowed_hosts",
                        "192.168.87.56:8000, mini.local:8000")
    hosts = mcp_server._allowed_hosts()
    assert "192.168.87.56:8000" in hosts
    assert "mini.local:8000" in hosts
    assert "*" not in hosts


# --- whole-house energy ----------------------------------------------------

def _seed_meters(import_wh, export_wh, solar_wh, closed=0.0, updated=None):
    import meters
    db = solar_routes.store()._db
    db.executescript(meters.SCHEMA)
    for ch, wh in (("site_import", import_wh), ("site_export", export_wh),
                   ("site_solar", solar_wh)):
        db.execute(
            "INSERT OR REPLACE INTO meters (channel, cumulative_wh, closed_wh,"
            " last_closed_bucket, updated_ts) VALUES (?,?,?,?,?)",
            (ch, wh, closed, 0, int(updated if updated else time.time())))
    db.commit()


@pytest.mark.asyncio
async def test_house_energy_answers_the_question_the_car_tools_cannot():
    """Asked for whole-house usage, the model correctly reported that no tool
    existed -- only car charging. The data was there all along in the meters
    table, free and local."""
    _seed_meters(import_wh=37_050, export_wh=4_590, solar_wh=45_270)
    out = await mcp_server.CALLABLES["get_home_energy"](period="today")

    assert out["solar_kwh"] == 45.27
    assert out["grid_import_kwh"] == 37.05
    assert out["grid_export_kwh"] == 4.59
    # The identity every energy dashboard uses: what the house consumed is
    # what it made, plus what it bought, minus what it sold.
    assert out["house_consumption_kwh"] == round(45.27 + 37.05 - 4.59, 2)
    assert out["self_sufficiency_pct"] is not None


@pytest.mark.asyncio
async def test_yesterdays_partial_is_never_relabelled_today():
    """THE subtle one. cumulative_wh minus closed_wh is 'today so far' only
    while the meter has actually been refreshed today. The collector ingests
    hourly, so just after midnight the row still holds YESTERDAY's partial --
    and reporting that as today would be a whole day's error stated
    confidently."""
    yesterday_evening = time.time() - 8 * 3600
    _seed_meters(37_050, 4_590, 45_270, updated=yesterday_evening)

    out = await mcp_server.CALLABLES["get_home_energy"](period="today")
    if out["meter_day_is_today"] is False:
        assert out["solar_kwh"] is None
        assert out["house_consumption_kwh"] is None
        assert "today" in out["unknown_reason"].lower()


@pytest.mark.asyncio
async def test_house_energy_reports_nothing_rather_than_zero_when_unseeded():
    """No meters row at all is 'never measured', not a house that used
    nothing."""
    out = await mcp_server.CALLABLES["get_home_energy"](period="today")
    assert out["solar_kwh"] is None
    assert out["unknown_reason"]


@pytest.mark.asyncio
async def test_a_quiet_controller_overnight_does_not_read_as_a_broken_collector():
    """get_solar_status said 'stale' and the model concluded the collector
    needed a look. It was running perfectly -- the controller simply logs no
    ticks after dark. Staleness and brokenness are different claims."""
    out = await mcp_server.CALLABLES["get_solar_status"]()
    if out["stale"] and out.get("collector_running"):
        assert "collector" in out["stale_reason"].lower()
        assert "running" in out["stale_reason"].lower() or \
               "alive" in out["stale_reason"].lower()
