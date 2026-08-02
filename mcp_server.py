"""The MCP surface: what an LLM may ask this system, and what it may change.

THE COST RULE, restated for this module. ha_routes proves "no read can spend
a Tesla request" by never importing the client, asserted with an AST test.
That cannot work here -- set_charge_mode and wake_car legitimately command the
car. The invariant is therefore re-cut PER TOOL: every read resolves from
SQLite, and tests/test_mcp_server.py runs each one against a client that
raises on any attribute access.

WHAT IS DELIBERATELY ABSENT is as much of the design as what is here. There is
no open_garage (a garage an injected prompt can open is a physical-security
exposure, and the arrival automation already covers the legitimate case), no
door_unlock or any other confirm=True command from commands.py, no
trigger_homelink (a blind stateless toggle is how a system ends up believing a
door is closed when it is not), no tunable writes (HA has bounded sliders and
there is no voice case for "set my export margin to 250 watts"), and no
garage_url or home coordinates (an SSRF primitive and a retroactive
redefinition of what counted as a home charge).

Seven tools is also a usability number: a short list is what makes a model
pick the right one.
"""
from __future__ import annotations

import time
from typing import Any

# mcp 2.0 renamed FastMCP to MCPServer and moved it to mcp.server. The class
# is the same shape the plan assumed: .tool(), .session_manager and
# .streamable_http_app() all behave as before.
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import car_routes
import green
import ha_routes
import solar
import solar_routes
from config import settings

mcp = MCPServer("tesla")

# Windows, and the label each reports. `today` is the calendar day and `week`
# is a rolling seven -- deliberately different, because each matches the
# question it answers. Every response names the window it used so the model
# never infers.
PERIODS = {
    "today": ("since local midnight", lambda now: solar_routes._midnight_ts()),
    "week":  ("rolling 7 days", lambda now: int(now) - 7 * 86400),
    "all":   ("lifetime", lambda now: 0),
}

READ_TOOLS = ("get_car_status", "get_charging_summary", "get_solar_status",
              "get_garage_status")

CALLABLES: dict[str, Any] = {}


def _client():
    from app import client
    return client


@mcp.tool()
async def get_car_status() -> dict[str, Any]:
    """Battery, range, charging state and where the car is.

    Every value carries its age. A sleeping car is normal and its data is
    hours old by design; presenting that as current is the one thing this
    project never does.
    """
    st = await ha_routes.ha_state()
    return {
        "soc_pct": st.get("soc"),
        "charge_limit_pct": st.get("limit"),
        "range_mi": st.get("range_mi"),
        "odometer_mi": st.get("odometer_mi"),
        "plugged_in": st.get("plugged_in"),
        "charging": st.get("charging"),
        "charging_state": st.get("charging_state"),
        # "home" | "away" | "unknown". Three-valued on purpose: Tesla OMITS
        # location keys rather than nulling them, so "scope revoked",
        # "sharing off" and "genuinely elsewhere" arrive identically.
        "location": st.get("location"),
        "data_age_s": st.get("snapshot_age_s"),
        "collector_running": st.get("collector_running"),
    }


CALLABLES["get_car_status"] = get_car_status


@mcp.tool()
async def get_charging_summary(period: str = "today") -> dict[str, Any]:
    """Daily/weekly/lifetime TOTALS: energy and miles added to the car, split
    into solar and grid.

    This is the tool for "how much did we add today", "how many solar miles
    did we add", "what share of this week's charging was solar". Use it for
    anything cumulative -- get_solar_status is instantaneous only and cannot
    answer a question about a period.

    `period` is "today" (calendar day), "week" (rolling 7 days) or "all".
    """
    if period not in PERIODS:
        raise ValueError(f"period must be one of {sorted(PERIODS)}")
    label, since_of = PERIODS[period]
    now = time.time()
    db = solar_routes.store()._db
    vin = solar_routes._vin()

    ticks = solar_routes._solar_ticks(db, vin, since_of(now))
    last_ts = solar.last_tick_ts(db, vin) if vin else None
    state = solar.load_state(db, vin) if vin else dict(solar.STATE_DEFAULTS)
    conf = solar.load_config(db)

    common = {
        "window": label,
        # How many controller ticks were actually LOGGED in this window. The
        # denominator behind every figure below, exposed because zero of them
        # and a quiet day are different claims that used to look identical.
        "observed_ticks": len(ticks),
        "collector_running": ha_routes.collector_running(state, now),
        "last_tick_age_s": int(now - last_ts) if last_ts else None,
        "charge_mode": solar.charge_mode(conf, now),
    }

    if not ticks:
        # NOTHING WAS OBSERVED. Every energy figure here is derived from
        # solar_ticks, so with no rows the honest answer is "unknown", not
        # zero -- the collector being down and the car not charging produce
        # an identical empty table, and only one of them is a real 0.0.
        # Reporting 0.0 with basis "measured" is the exact failure this
        # project refuses everywhere else.
        stale = (f"; the last controller tick was {int((now - last_ts) / 3600)}h ago"
                 if last_ts else "; no tick has ever been recorded")
        return {
            **common,
            "solar_kwh": None, "grid_kwh": None, "total_kwh": None,
            "solar_share_pct": None, "miles_added": None, "solar_miles": None,
            "miles_basis": None,
            "unknown_reason": (
                f"no controller ticks were recorded {label}{stale}, so nothing "
                "was measured in this window. This is not a zero -- an idle car "
                "and a stopped collector leave the same empty record. Check "
                "that the collector is running."),
        }

    solar_kwh, grid_kwh = green.charged_split(ticks)

    sessions, segments = solar_routes._sessions_and_segments(db, vin)
    pack, _ = green.pack_kwh(sessions)
    mpk, sampled = green.miles_per_kwh(segments, pack)

    total = solar_kwh + grid_kwh
    return {
        **common,
        "solar_kwh": round(solar_kwh, 2),
        "grid_kwh": round(grid_kwh, 2),
        "total_kwh": round(total, 2),
        "solar_share_pct": round(100 * solar_kwh / total, 1) if total > 0 else None,
        "miles_added": round(total * mpk, 1) if mpk else None,
        "solar_miles": round(solar_kwh * mpk, 1) if mpk else None,
        # "measured" only, never the rated/nominal-pack fallback: that basis
        # and the banked-miles basis disagree ~30% on this car, and a figure
        # resting on a guess must not sit beside one that does not.
        "miles_basis": "measured" if mpk else None,
        "unknown_reason": None if mpk else (
            f"only {sampled:.0f} miles sampled so far; need more driving "
            "history before energy can be converted to miles"),
    }


CALLABLES["get_charging_summary"] = get_charging_summary


@mcp.tool()
async def get_solar_status() -> dict[str, Any]:
    """INSTANTANEOUS power at the house, as of the last controller tick.

    A snapshot, never a total: this cannot answer "how much today" or
    "how many miles" -- get_charging_summary does that.

    Every reading here is only as current as `data_age_s`. When `stale` is
    true the numbers are historical and must be reported with their age, not
    as "right now" -- a stopped collector leaves the last tick sitting here
    looking exactly like a live one.
    """
    st = await ha_routes.ha_state()
    now = time.time()
    last_tick_ts = st.get("last_tick_ts")
    age_s = int(now - last_tick_ts) if last_tick_ts else None
    # Believe a reading for two of the collector's own slowest sleeps. Past
    # that it is history. Same principle as ha_routes.collector_running:
    # judged against the cadence in force, not a constant.
    stale = age_s is None or age_s > 3600 or not st.get("collector_running")
    return {
        "data_age_s": age_s,
        "stale": stale,
        "stale_reason": None if not stale else (
            "no tick has ever been recorded" if age_s is None else
            f"the last controller tick was {age_s // 3600}h {age_s % 3600 // 60}m "
            "ago; these are historical readings, not current ones"),
        "solar_w": st.get("solar_w"),
        "grid_import_w": st.get("grid_import_w"),
        "grid_export_w": st.get("grid_export_w"),
        "house_w": st.get("house_w"),
        "car_w": st.get("car_w"),
        "surplus_w": st.get("surplus_w"),
        "controller_state": st.get("state"),
        "amps": st.get("amps"),
        "charge_mode": solar.charge_mode(
            solar.load_config(solar_routes.store()._db), time.time()),
        "rate_limited": st.get("rate_limited"),
        "daily_cap_reached": st.get("capped"),
        "restore_pending": st.get("dirty"),
        "ledger_stale": st.get("ledger_stale"),
        "last_tick_ts": st.get("last_tick_ts"),
        "collector_running": st.get("collector_running"),
    }


CALLABLES["get_solar_status"] = get_solar_status


async def _garage_snapshot() -> dict[str, Any]:
    """Indirection so the test can substitute an unreachable opener.

    Named differently from solar_routes._garage_reading(url), which takes a
    URL and is a different function.
    """
    return await solar_routes.get_garage()


@mcp.tool()
async def get_garage_status() -> dict[str, Any]:
    """Whether the garage door is open, closed, or unknown.

    An unreachable opener reports reachable: false and a null door state. It
    must NEVER read as "Closed" -- that is a lie the owner would act on, and
    the single worst failure this system can produce.
    """
    reading = await _garage_snapshot()
    return {
        "reachable": bool(reading.get("reachable")),
        "door_state": reading.get("door_state"),
        "obstructed": reading.get("obstructed"),
    }


CALLABLES["get_garage_status"] = get_garage_status


@mcp.tool()
async def set_charge_mode(mode: str) -> dict[str, Any]:
    """Set how the car charges.

    "solar" charges only from surplus sun. "now" charges immediately at full
    rate to the existing charge limit, and reverts to solar at local midnight
    or when charging ends, whichever comes first. "off" disables automatic
    charging entirely.

    "now" may wake a sleeping car, which costs one billed request.
    """
    return await solar_routes.put_charge_mode({"mode": mode})


CALLABLES["set_charge_mode"] = set_charge_mode


@mcp.tool()
async def close_garage() -> dict[str, Any]:
    """Close the garage door, with the unattended-close safety sequence."""
    # NOT solar_routes.post_garage_close(). That endpoint is documented as
    # "the owner is present and just pressed it" and closes immediately with
    # no warning -- closing is the one irreversible direction and can trap a
    # person, a pet or a bicycle. An MCP call is not a person standing there,
    # so it must route to the warned path: safe_to_close() gate, light on,
    # garage_close_warn_s, re-read, abort on obstruction.
    #
    # That endpoint is spec task 9 and is blocked on the macOS Local Network
    # grant. Refusing loudly is correct until it exists; silently calling the
    # blunt close is exactly the failure garage.py's docstring warns about.
    return {
        "ok": False,
        "reason": "the unattended-close path is not built yet (spec task 9, "
                  "blocked on the macOS Local Network grant). Use the car "
                  "page's manual button, where you are present.",
    }


CALLABLES["close_garage"] = close_garage


@mcp.tool()
async def wake_car() -> dict[str, Any]:
    """Wake the car so it can be commanded. Costs one billed request and is
    rate-limited to once a minute."""
    return await car_routes.car_wake()


CALLABLES["wake_car"] = wake_car


def _allowed_hosts() -> list[str]:
    """Host header values /mcp will answer to.

    The SDK ships DNS-rebinding protection on, with an allow-list that in
    practice means 127.0.0.1. A request from another machine arrives with
    `Host: 192.168.x.y:8000` and is refused 421 Misdirected Request -- which
    is exactly the case this endpoint exists for, Claude Desktop on a laptop
    talking to the box that holds the data.

    So the list is EXTENDED, never disabled: loopback plus whatever the owner
    names in MCP_ALLOWED_HOSTS. The Host header includes the port, so each
    entry needs one. A wildcard is deliberately not supported -- the token is
    the primary defence, and this is the cheap second one.
    """
    port = settings.port
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"]
    for extra in settings.mcp_allowed_hosts.split(","):
        extra = extra.strip()
        if extra and extra not in hosts:
            hosts.append(extra)
    return hosts


# Built once, at import, because app.py mounts it at import and the session
# manager behind it may be run only once per instance. streamable_http_path
# is "/" rather than the SDK's default "/mcp": the mount already supplies
# that prefix, and leaving the default would serve the endpoint at /mcp/mcp.
http_app = mcp.streamable_http_app(
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(allowed_hosts=_allowed_hosts()),
)
