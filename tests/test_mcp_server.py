from __future__ import annotations

import pytest

import ha_routes
import mcp_server
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
        assert out["miles_unknown_reason"]


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
