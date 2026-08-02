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


def test_the_tool_list_is_exactly_the_seven_specified():
    assert set(mcp_server.CALLABLES) == {
        "get_car_status", "get_charging_summary", "get_solar_status",
        "get_garage_status", "set_charge_mode", "close_garage", "wake_car"}


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
    now = int(time.time())
    _seed_ticks([(now - 600, 0.0, 300.0), (now - 480, 0.0, 310.0)])

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
