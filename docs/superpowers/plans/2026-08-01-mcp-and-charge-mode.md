# MCP Server and `charge_mode` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give this service an authenticated MCP endpoint with seven tools, and a `charge_mode` the collector honours, so Claude can answer questions about the car and start a charge without the solar control loop immediately undoing it.

**Architecture:** `charge_mode` is derived from one new config column (`force_charge_until`) plus one new state column (`force_started`); `enabled` keeps its exact current meaning. All new decision logic is pure functions in `solar.py`, tested without network, and wired into `collector.py` at four call sites. `mcp_server.py` mounts into the existing FastAPI app and calls Python functions directly — reads through the SQLite-only helpers, writes through `car_routes`, where the daily request cap lives.

**Tech Stack:** Python 3.13.7, FastAPI 0.115.6, uvicorn 0.34.0, httpx 0.28.1, SQLite (WAL), pytest 8 + pytest-asyncio, and one new dependency: `mcp` (the official Python SDK).

**Spec:** `docs/superpowers/specs/2026-08-01-mcp-and-voice-control-design.md`. This plan covers **spec tasks 1–7 only**. Spec tasks 8–10 (HA entities, garage cover, Google Smart Home Action) are configuration on servy and in Google's console and get their own plan.

## Global Constraints

- **No read may spend a Tesla request.** Every MCP read tool resolves from SQLite. The per-tool test in Task 6 enforces this with a client mock that fails the test if called.
- **`ha_routes.py` must never import `tesla`.** Enforced by the existing AST test in `tests/test_ha_routes.py`. Do not weaken it.
- **Routers mount above the static catch-all.** `app.py:289` mounts `StaticFiles` at `/`; anything included after it is unreachable.
- **`CREATE TABLE IF NOT EXISTS` never reaches a live `car.db`.** Every new column needs an entry in `STATE_NEW_COLUMNS` or `CONFIG_NEW_COLUMNS` *and* in the `SCHEMA` string. Both, always.
- **Writer separation:** `solar_config` is written by the web app, `solar_state` by the collector. Task 4 is the first code to write `force_started` from the collector — that is `solar_state`, so the rule holds. Do not write `solar_config` from the collector.
- **`null` never `0`** for anything HA consumes as a meter.
- **Fail closed.** An unknown door state, an unknown location, an unreadable device: do nothing, and say so.
- Commit messages: lowercase `type: subject`, no trailing period.

---

## File Structure

| File | Responsibility |
|---|---|
| `auth.py` *(new)* | Token + CSRF middleware. Nothing else. |
| `config.py` | Gains three token fields. |
| `solar.py` | Gains `force_charge_until` / `force_started` schema, and four pure functions: `forcing`, `charge_mode`, `next_midnight_ts`, `force_plan`, `force_expired`. |
| `collector.py` | Four wiring points for force mode. No new decision logic. |
| `solar_routes.py` | `GET`/`PUT /api/car/charge-mode`. |
| `car_routes.py` | `_spend()` in `car_command`; wake rate limit. |
| `mcp_server.py` *(new)* | The seven tools and the FastMCP app. Thin — every tool is a call into an existing helper plus a response shape. |
| `tests/test_auth.py` *(new)* | Middleware behaviour. |
| `tests/test_charge_mode.py` *(new)* | The pure force-mode functions. |
| `tests/test_mcp_server.py` *(new)* | Tool contracts and the per-tool cost assertion. |

---

## Task 1: Auth middleware

**Files:**
- Create: `auth.py`
- Create: `tests/test_auth.py`
- Modify: `config.py:89` (after `currency`)
- Modify: `app.py:65` (after `app = FastAPI(...)`)
- Modify: `.env.example`

**Interfaces:**
- Consumes: `config.settings`
- Produces: `auth.install(app)`; `settings.api_token`, `settings.api_token_ha`, `settings.api_token_mcp`

### A decision this task forces

`HOST` is bound for LAN access (commit `7d3c6a2`). Guarding `/api/*` by token alone breaks the dashboard for any browser that is not on the mini, because a page load cannot send `X-Api-Key`. So the middleware accepts the token from **either** the header **or** an `api_token` cookie, and a tiny `GET /auth/ui?token=…` sets that cookie. The cookie is CSRF-able by design — which is exactly why the `Sec-Fetch-Site` check below is not optional.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth.py`:

```python
from __future__ import annotations

import auth


def _req(path="/api/car/home", method="GET", host="192.168.87.50",
         headers=None):
    """A stand-in for the parts of a Starlette Request that auth.decide reads."""
    return auth.Incoming(path=path, method=method, client_host=host,
                         headers=headers or {})


def test_loopback_reads_need_no_token():
    assert auth.decide(_req(host="127.0.0.1"), tokens=("secret",)) is None


def test_lan_reads_need_a_token():
    assert auth.decide(_req(), tokens=("secret",)) == (401, "unauthorized")


def test_lan_reads_pass_with_the_header():
    assert auth.decide(
        _req(headers={"x-api-key": "secret"}), tokens=("secret",)) is None


def test_lan_reads_pass_with_the_cookie():
    assert auth.decide(
        _req(headers={"cookie": "api_token=secret"}), tokens=("secret",)) is None


def test_any_of_the_three_tokens_is_accepted():
    assert auth.decide(
        _req(headers={"x-api-key": "mcp"}),
        tokens=("browser", "ha", "mcp")) is None


def test_an_unset_token_is_never_a_valid_credential():
    """Three tokens are configured as ('secret', '', ''). An empty presented
    value must not match the empty slots -- that would make an absent header
    a valid credential the moment any one token is left unconfigured."""
    assert auth.decide(
        _req(headers={"x-api-key": ""}), tokens=("secret", "", "")
    ) == (401, "unauthorized")


def test_ha_and_mcp_need_a_token_even_from_loopback():
    """These are control surfaces, not the local UI."""
    for path in ("/api/ha/state", "/mcp"):
        assert auth.decide(
            _req(path=path, host="127.0.0.1"), tokens=("secret",)
        ) == (401, "unauthorized"), path


def test_cross_site_mutations_are_refused_even_from_loopback():
    """The drive-by CSRF form targets 127.0.0.1:8000 from the owner's OWN
    browser, so the loopback exemption alone does not close it."""
    assert auth.decide(
        _req(path="/api/car/wake", method="POST", host="127.0.0.1",
             headers={"sec-fetch-site": "cross-site"}),
        tokens=("secret",)) == (403, "cross-site request refused")


def test_same_origin_mutations_are_allowed():
    assert auth.decide(
        _req(path="/api/car/wake", method="POST", host="127.0.0.1",
             headers={"sec-fetch-site": "same-origin"}),
        tokens=("secret",)) is None


def test_a_missing_sec_fetch_site_is_not_treated_as_cross_site():
    """curl and every non-browser client omit it. Refusing on absence would
    break the CLI and every automation without closing anything -- a browser
    that can forge the header can forge anything."""
    assert auth.decide(
        _req(path="/api/car/wake", method="POST", host="127.0.0.1"),
        tokens=("secret",)) is None


def test_auth_routes_stay_open_from_loopback():
    """Tesla redirects the browser to /auth/callback with the code; a 401
    there strands the whole OAuth flow."""
    assert auth.decide(
        _req(path="/auth/callback", host="127.0.0.1"), tokens=("secret",)) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'auth'`

- [ ] **Step 3: Write `auth.py`**

```python
"""Who may call this service.

Two independent checks, because they close two different holes.

TOKEN, for anything not on the loopback interface. The dashboard binds to the
LAN (commit 7d3c6a2), which means the garage opener and the config writes are
reachable from every device on the network. A shared secret is the whole
defence.

SEC-FETCH-SITE, on every mutating method REGARDLESS of source address. The
attack the token does not stop is a drive-by form on any web page the owner
visits, submitting to 127.0.0.1:8000 from the owner's own browser -- which
arrives on loopback and would be exempt. Browsers set Sec-Fetch-Site on every
request and it cannot be forged by script, so a value present and not
same-origin is a refusal. ABSENCE is not: curl, the collector and every
non-browser client omit it entirely.

The cookie exists so the LAN browser UI keeps working -- a page load cannot
send X-Api-Key. It is deliberately CSRF-able, and the Sec-Fetch-Site check
above is what makes that safe.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from http.cookies import SimpleCookie

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import settings

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Control surfaces: a token is required even from loopback. These are not the
# local UI, and nothing legitimate reaches them from a browser page load.
GUARDED = ("/api/ha/", "/mcp")

# The OAuth dance lands the browser here with Tesla's code. A 401 would
# strand it with no way to finish authorising.
OPEN = ("/auth/",)


@dataclass(frozen=True)
class Incoming:
    """The parts of a request this module judges. A plain value so the
    decision is a pure function and testable without a live app."""
    path: str
    method: str
    client_host: str | None
    headers: dict = field(default_factory=dict)


def _presented(req: Incoming) -> str:
    header = req.headers.get("x-api-key")
    if header:
        return header
    raw = req.headers.get("cookie")
    if not raw:
        return ""
    try:
        jar = SimpleCookie()
        jar.load(raw)
    except Exception:
        return ""
    morsel = jar.get("api_token")
    return morsel.value if morsel else ""


def _token_ok(presented: str, tokens: tuple[str, ...]) -> bool:
    """Constant-time comparison against every configured token.

    An empty `presented` is refused BEFORE the loop. Without that, a
    deployment with only one of the three tokens configured would have the
    other two as empty strings, and an absent header would compare equal to
    them -- turning a partial configuration into no authentication at all.
    """
    if not presented:
        return False
    ok = False
    for token in tokens:
        if token and secrets.compare_digest(presented, token):
            ok = True
    return ok


def decide(req: Incoming, tokens: tuple[str, ...]) -> tuple[int, str] | None:
    """None to allow, or (status, detail) to refuse. Pure."""
    if req.method in MUTATING:
        site = req.headers.get("sec-fetch-site")
        if site is not None and site != "same-origin":
            return 403, "cross-site request refused"

    if req.path.startswith(OPEN):
        return None

    guarded = req.path.startswith(GUARDED)
    if not guarded and req.client_host in LOOPBACK:
        return None

    if _token_ok(_presented(req), tokens):
        return None
    return 401, "unauthorized"


def _tokens() -> tuple[str, ...]:
    return (settings.api_token, settings.api_token_ha, settings.api_token_mcp)


def install(app: FastAPI) -> None:
    """Wire the middleware and the cookie-setting route onto `app`."""

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        verdict = decide(
            Incoming(
                path=request.url.path,
                method=request.method,
                client_host=request.client.host if request.client else None,
                # Starlette headers are already lower-cased on lookup, but a
                # plain dict is what `decide` takes, so normalise here.
                headers={k.lower(): v for k, v in request.headers.items()},
            ),
            _tokens(),
        )
        if verdict is not None:
            status, detail = verdict
            return JSONResponse({"detail": detail}, status_code=status)
        return await call_next(request)

    @app.get("/auth/ui")
    async def _set_ui_cookie(token: str = ""):
        """Trade a token in the query string for a cookie, so the LAN browser
        UI works. Loopback browsers never need this."""
        if not _token_ok(token, _tokens()):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "api_token", token, httponly=True, samesite="strict", max_age=31536000)
        return response
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_auth.py -v`
Expected: 11 passed

- [ ] **Step 5: Add the token settings**

In `config.py`, after the `currency` field (line 89), add:

```python
    # Three separate secrets so each consumer rotates independently: the
    # browser UI, Home Assistant, and the MCP endpoint. A leaked HA token
    # must not also be an MCP credential.
    api_token: str = field(default_factory=lambda: _clean(os.getenv("API_TOKEN")))
    api_token_ha: str = field(default_factory=lambda: _clean(os.getenv("API_TOKEN_HA")))
    api_token_mcp: str = field(default_factory=lambda: _clean(os.getenv("API_TOKEN_MCP")))
```

- [ ] **Step 6: Wire it into the app**

In `app.py`, immediately after `app = FastAPI(title="Tesla Energy Dashboard", lifespan=lifespan)` (line 65), add:

```python
import auth
auth.install(app)
```

Move the `import auth` up to the import block at the top with the other local imports (`car_routes`, `demo`, `energy`, …) — this shows it inline only so the placement of the `install` call is unambiguous.

- [ ] **Step 7: Document the env vars**

Append to `.env.example`:

```
# Shared secrets. Generate each with: openssl rand -hex 32
# Without these, every non-loopback request is refused (fail closed) and
# /api/ha/* and /mcp are refused even from loopback.
API_TOKEN=
API_TOKEN_HA=
API_TOKEN_MCP=
```

- [ ] **Step 8: Run the WHOLE suite and fix the fallout**

Run: `.venv/bin/python -m pytest -q`

Expected: some existing tests that drive the app through `TestClient` now return 401 or 403. This is the middleware working. For each failure, fix the **test**, not the middleware:
- `TestClient(app)` sets no client host; Starlette reports `testclient`, which is not in `LOOPBACK`, so add `headers={"X-Api-Key": "…"}` and set the token via monkeypatch, **or** construct the client as `TestClient(app, client=("127.0.0.1", 1234))`.
- Prefer the second: it exercises the loopback path the browser UI actually uses.

Record the count of tests touched in the commit message.

- [ ] **Step 9: Commit**

```bash
git add auth.py tests/test_auth.py config.py app.py .env.example
git commit -m "feat: token and CSRF middleware

Non-loopback requests need a token; /api/ha/* and /mcp need one even from
loopback. Every mutating method refuses a present-and-not-same-origin
Sec-Fetch-Site regardless of source address -- the drive-by CSRF form
targets 127.0.0.1 from the owner's own browser, so the loopback exemption
alone does not close it.

A cookie alternative to the header keeps the LAN browser UI working, and
is safe only because of the Sec-Fetch-Site check."
```

---

## Task 2: Close the two cost leaks

**Files:**
- Modify: `car_routes.py:170-216` (`car_command`), `car_routes.py:153-161` (`car_wake`)
- Modify: `tests/test_car_routes.py`

**Interfaces:**
- Consumes: `car_routes._spend(vin)` (already exists, `car_routes.py:133`)
- Produces: no new public names

The spec's Task 2 has three parts. `TESLA_VIN` is an owner action recorded in §8 of the spec, not code. The two code parts:

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_car_routes.py`:

```python
import pytest

import car_routes
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_a_command_is_counted_against_the_daily_cap(monkeypatch):
    """The cap guarded only the collector. POST /api/car/command/{id} spent
    entirely outside it, so requests_today reported a comfortable number
    while the budget drained through a door it did not watch."""
    spent = []
    monkeypatch.setattr(car_routes, "_spend", lambda vin: spent.append(vin))
    monkeypatch.setattr(car_routes, "DEMO", False)
    monkeypatch.setattr(car_routes, "_vin", _fake_vin)
    monkeypatch.setattr(car_routes, "_client", _fake_client)

    await car_routes.car_command("flash_lights", {})
    assert spent == ["5YJSA00000F000000"], "the command did not book a request"


@pytest.mark.asyncio
async def test_a_second_wake_inside_the_window_is_refused(monkeypatch):
    """A wake is $0.02 -- the most expensive request this system makes --
    against a $10/month credit. Nothing stopped a button from being held
    down."""
    monkeypatch.setattr(car_routes, "_spend", lambda vin: None)
    monkeypatch.setattr(car_routes, "DEMO", False)
    monkeypatch.setattr(car_routes, "_vin", _fake_vin)
    monkeypatch.setattr(car_routes, "_client", _fake_client)
    monkeypatch.setattr(car_routes, "_last_wake_ts", 0.0)

    await car_routes.car_wake()
    with pytest.raises(HTTPException) as exc:
        await car_routes.car_wake()
    assert exc.value.status_code == 429
```

Add these helpers at the top of the file (or reuse existing equivalents if the file already has them — check first, and do not duplicate):

```python
async def _fake_vin():
    return "5YJSA00000F000000"


class _FakeClient:
    def __init__(self):
        self.cache = type("C", (), {"clear": lambda self: None})()

    async def command(self, vin, cmd_id, body):
        return 200, {"response": {"result": True}}

    async def wake_up(self, vin):
        return {"state": "online"}


def _fake_client():
    return _FakeClient()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_car_routes.py -k "daily_cap or inside_the_window" -v`
Expected: FAIL — `spent == []` for the first, no 429 for the second.

- [ ] **Step 3: Book the command against the cap**

In `car_routes.py`, inside `car_command`, immediately after `vin = await _vin()` (line 184) and **before** the `needs_location` block:

```python
    # Counted BEFORE the call, exactly as _spend's docstring requires: a
    # request about to be made is already committed, and counting on the way
    # back would let a hung call be retried past the cap for free.
    _spend(vin)
```

- [ ] **Step 4: Rate-limit the wake**

In `car_routes.py`, add a module-level variable next to `_store` (line 37):

```python
# A wake is $0.02 against a $10/month credit -- 20x a command. One per
# minute is generous for a human pressing a button and closes a held-down
# key or a retrying client. Process-local by design: this guards the cost of
# an impulse, and a restart is not an impulse.
_last_wake_ts: float = 0.0
WAKE_MIN_INTERVAL_S = 60
```

Then in `car_wake`, after `vin = await _vin()` and before `_spend(vin)`:

```python
    global _last_wake_ts
    since = time.time() - _last_wake_ts
    if since < WAKE_MIN_INTERVAL_S:
        raise HTTPException(
            429, f"wake rate-limited; retry in {int(WAKE_MIN_INTERVAL_S - since)}s")
    _last_wake_ts = time.time()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_car_routes.py -v`
Expected: PASS, including the pre-existing tests in that file.

- [ ] **Step 6: Commit**

```bash
git add car_routes.py tests/test_car_routes.py
git commit -m "fix: two cost leaks the HA design named and never closed

POST /api/car/command/{id} spent Tesla requests entirely outside
daily_request_cap, so requests_today reported a comfortable number while
the budget drained through a door it did not watch. And nothing stopped a
wake -- the most expensive request this system makes -- from being issued
as fast as a button could be pressed."
```

---

## Task 3: `force_charge_until` schema and the pure mode functions

**Files:**
- Modify: `solar.py:386-521` (SCHEMA, CONFIG_DEFAULTS, STATE_DEFAULTS), `solar.py:558-613` (the two `*_NEW_COLUMNS` tuples)
- Create: `tests/test_charge_mode.py`

**Interfaces:**
- Produces:
  - `solar.forcing(conf: dict, now: float) -> bool`
  - `solar.charge_mode(conf: dict, now: float) -> str` — `"now" | "solar" | "off"`
  - `solar.next_midnight_ts(tz: str, now: float) -> int`
  - `solar.force_plan(*, state, location, plugged, car_charging, amps_actual, amps_max, force_started) -> tuple[list[str], bool]`
  - `solar.force_expired(*, force_charge_until, now, car_charging, force_started) -> bool`
  - New columns: `solar_config.force_charge_until INTEGER`, `solar_state.force_started INTEGER NOT NULL DEFAULT 0`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_charge_mode.py`:

```python
from __future__ import annotations

import sqlite3

import solar

TZ = "America/Los_Angeles"


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(solar.SCHEMA)
    solar.migrate_state(db)
    solar.migrate_config(db)
    return db


# --- mode derivation -------------------------------------------------------

def test_mode_is_off_when_nothing_is_enabled():
    assert solar.charge_mode({"enabled": 0, "force_charge_until": None}, 1000) == "off"


def test_mode_is_solar_when_enabled():
    assert solar.charge_mode({"enabled": 1, "force_charge_until": None}, 1000) == "solar"


def test_force_outranks_enabled_while_it_is_live():
    conf = {"enabled": 1, "force_charge_until": 2000}
    assert solar.charge_mode(conf, 1000) == "now"


def test_an_expired_force_falls_back_to_the_underlying_mode():
    """The column is not cleared by the passage of time -- only by the
    collector, on its next tick. Every reader must therefore compare against
    the clock rather than trusting the column's presence."""
    conf = {"enabled": 1, "force_charge_until": 2000}
    assert solar.charge_mode(conf, 2000) == "solar"
    assert solar.charge_mode({"enabled": 0, "force_charge_until": 2000}, 2000) == "off"


def test_next_midnight_is_the_next_one_not_todays():
    # 2026-08-01 12:00 local -> 2026-08-02 00:00 local
    noon = 1785697200  # 2026-08-01T12:00:00-07:00
    assert solar.next_midnight_ts(TZ, noon) == 1785740400  # 2026-08-02T00:00-07:00


def test_a_force_set_just_before_midnight_expires_in_minutes_not_a_day():
    """Accepted behaviour, recorded so it is never mistaken for a bug: the
    owner chose midnight expiry, and 23:50 is ten minutes from midnight."""
    late = 1785740400 - 600           # 2026-08-01T23:50 local
    assert solar.next_midnight_ts(TZ, late) - late == 600


# --- force_plan ------------------------------------------------------------

def test_a_cold_start_commands_start_and_amps():
    actions, started = solar.force_plan(
        state="idle", location="home", plugged=True, car_charging=False,
        amps_actual=0, amps_max=48, force_started=False)
    assert actions == ["force_start", "force_amps"]
    assert started is False, "not started until charging is actually observed"


def test_the_latch_is_set_only_once_charging_is_observed():
    actions, started = solar.force_plan(
        state="idle", location="home", plugged=True, car_charging=True,
        amps_actual=48, amps_max=48, force_started=False)
    assert actions == [], "already at the target -- commanding again is waste"
    assert started is True


def test_amps_are_rewritten_when_the_car_is_below_the_target():
    actions, started = solar.force_plan(
        state="idle", location="home", plugged=True, car_charging=True,
        amps_actual=5, amps_max=48, force_started=True)
    assert actions == ["force_amps"]
    assert started is True


def test_a_charge_that_ended_after_starting_commands_nothing():
    """This is the expiry signal. force_plan must not try to restart it --
    that would fight the owner's own stop and loop forever."""
    actions, started = solar.force_plan(
        state="idle", location="home", plugged=True, car_charging=False,
        amps_actual=0, amps_max=48, force_started=True)
    assert actions == []
    assert started is True


def test_an_engaged_solar_machine_is_handed_off_before_forcing():
    """The controller may be holding the car at its 5 A floor with dirty set.
    Forcing on top of that would leave original_amps pointing at a value the
    restore path can never make good on."""
    actions, _ = solar.force_plan(
        state="charging", location="home", plugged=True, car_charging=True,
        amps_actual=5, amps_max=48, force_started=False)
    assert actions == ["restore"]


def test_unknown_location_freezes_exactly_as_advance_does():
    """Tesla OMITS location keys rather than nulling them, so 'scope revoked',
    'sharing off' and 'genuinely elsewhere' are indistinguishable. Commanding
    a car we cannot place is not acceptable in either machine."""
    actions, _ = solar.force_plan(
        state="idle", location="unknown", plugged=True, car_charging=False,
        amps_actual=0, amps_max=48, force_started=False)
    assert actions == []


def test_an_unplugged_car_commands_nothing():
    actions, _ = solar.force_plan(
        state="idle", location="home", plugged=False, car_charging=False,
        amps_actual=0, amps_max=48, force_started=False)
    assert actions == []


def test_a_car_away_from_home_commands_nothing():
    actions, _ = solar.force_plan(
        state="idle", location="away", plugged=True, car_charging=False,
        amps_actual=0, amps_max=48, force_started=False)
    assert actions == []


# --- force_expired ---------------------------------------------------------

def test_midnight_expires_the_mode():
    assert solar.force_expired(
        force_charge_until=2000, now=2000, car_charging=True, force_started=True)


def test_charging_ending_after_it_started_expires_the_mode():
    """Reaching the charge limit reports Complete, which is the success case."""
    assert solar.force_expired(
        force_charge_until=9999, now=1000, car_charging=False, force_started=True)


def test_the_first_tick_does_not_expire_the_mode():
    """THE reason force_started exists. Right after charge_start the car
    reports Starting, or briefly still Stopped; without the latch the mode
    would expire on its own first tick and never charge anything."""
    assert not solar.force_expired(
        force_charge_until=9999, now=1000, car_charging=False, force_started=False)


def test_nothing_expires_when_nothing_is_forcing():
    assert not solar.force_expired(
        force_charge_until=None, now=1000, car_charging=False, force_started=True)


# --- schema ----------------------------------------------------------------

def test_the_new_columns_reach_a_database_that_predates_them():
    """CREATE TABLE IF NOT EXISTS is a no-op against the owner's live
    car.db. A schema edit alone never reaches it -- only the migration does."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE solar_config (id INTEGER PRIMARY KEY CHECK (id = 1),"
               " enabled INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE TABLE solar_state (vin TEXT PRIMARY KEY,"
               " updated_at INTEGER NOT NULL DEFAULT 0)")
    solar.migrate_config(db)
    solar.migrate_state(db)
    assert "force_charge_until" in {r[1] for r in db.execute("PRAGMA table_info(solar_config)")}
    assert "force_started" in {r[1] for r in db.execute("PRAGMA table_info(solar_state)")}


def test_force_charge_until_round_trips_as_null():
    """Absence is meaningful: NULL means 'not forcing', and a 0 would read as
    an expiry in 1970 -- indistinguishable here, but not in the API layer,
    where 0 is a legitimate integer a client could send."""
    db = _db()
    solar.save_config(db, force_charge_until=None)
    assert solar.load_config(db)["force_charge_until"] is None
    solar.save_config(db, force_charge_until=1785740400)
    assert solar.load_config(db)["force_charge_until"] == 1785740400
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_charge_mode.py -v`
Expected: FAIL — `AttributeError: module 'solar' has no attribute 'charge_mode'`

- [ ] **Step 3: Add the schema columns**

In `solar.py`, in the `SCHEMA` string, inside `CREATE TABLE IF NOT EXISTS solar_config`, after the `deadline_hour INTEGER,` line (line 419):

```sql
  -- charge_mode = "now": a unix timestamp the force expires at, NULL when
  -- not forcing. A TIMESTAMP rather than a flag plus a day-stamp, so an app
  -- that is down at midnight expires late on its next tick instead of
  -- missing the rollover entirely.
  force_charge_until  INTEGER,
```

Inside `CREATE TABLE IF NOT EXISTS solar_state`, after the `garage_last_close_day TEXT,` line (line 468):

```sql
  -- Have we yet OBSERVED the forced charge running? Right after charge_start
  -- the car reports Starting, or briefly still Stopped, so "not charging" is
  -- not evidence of an ended charge until this is set. Without it the mode
  -- expires on its own first tick.
  force_started         INTEGER NOT NULL DEFAULT 0,
```

- [ ] **Step 4: Add the migrations and defaults**

In `CONFIG_NEW_COLUMNS` (line 600), append:

```python
    ("force_charge_until", "INTEGER"),
```

In `STATE_NEW_COLUMNS` (line 558), append:

```python
    ("force_started", "INTEGER NOT NULL DEFAULT 0"),
```

In `CONFIG_DEFAULTS` (line 511), add `"force_charge_until": None,`.
In `STATE_DEFAULTS` (line 523), add `"force_started": 0,`.

- [ ] **Step 5: Add the pure functions**

In `solar.py`, immediately after `advance()` ends (line 383) and before the `SCHEMA` string, add:

```python
# --- charge_mode: the "now" override ---------------------------------------
#
# advance() above cannot express "charge regardless of the sun". With
# enabled=1 an externally started charge lands in idle with car_charging=True,
# is ADOPTED, has set_amps written against a negative night surplus, breaches
# the floor within two ticks, transits grace and stops -- about four billed
# commands to end exactly where it began. So force mode does not run advance()
# at all; it runs force_plan() instead, and the solar machine stays idle
# throughout.
#
# Mode is DERIVED, never stored: `enabled` keeps its exact meaning, so every
# existing test, the HA switch and the setup page stay correct.


def forcing(conf: dict, now: float) -> bool:
    """Whether a force is live right now.

    Compared against the clock, never merely tested for presence: the column
    is cleared by the collector on its next tick, so between expiry and that
    tick the timestamp is still there and still in the past.
    """
    until = conf.get("force_charge_until")
    return bool(until) and now < until


def charge_mode(conf: dict, now: float) -> str:
    """"now" | "solar" | "off"."""
    if forcing(conf, now):
        return "now"
    return "solar" if conf["enabled"] else "off"


def next_midnight_ts(tz: str, now: float) -> int:
    """The next local midnight strictly after `now`.

    Adding a day to the aware datetime BEFORE replacing the time-of-day is
    what makes this correct across a DST boundary: replace-then-add would
    build a wall-clock midnight that does not exist on a spring-forward day.
    """
    zone = ZoneInfo(tz)
    tomorrow = datetime.fromtimestamp(now, zone) + timedelta(days=1)
    return int(tomorrow.replace(hour=0, minute=0, second=0,
                                microsecond=0).timestamp())


def force_plan(*, state: str, location: str, plugged: bool,
               car_charging: bool, amps_actual: int, amps_max: int,
               force_started: bool) -> tuple[list[str], bool]:
    """What to do this tick while charge_mode is "now". Pure.

    Returns (actions, force_started_next). Actions are names, not calls --
    collector.py performs them, exactly as with advance().

    Order matters. The unknown-location freeze comes first for the same
    reason it does in advance(): Tesla OMITS location keys rather than
    nulling them, so "scope revoked", "sharing off" and "genuinely elsewhere"
    are indistinguishable, and none of them is grounds to command a car.

    The hand-off comes second. The solar controller may be mid-engagement,
    holding the car at its 5 A floor with `dirty` set and `original_amps`
    recorded. Forcing on top of that would overwrite the amps the restore
    path exists to put back.
    """
    if location == "unknown":
        return [], force_started
    if state != "idle":
        return ["restore"], force_started
    if not plugged or location != "home":
        return [], force_started
    if car_charging:
        # Only write when the car is not already where we want it. The
        # controller's own "already holds this value" suppression, applied
        # here: steady state is zero commands per tick, not one every 120 s.
        actions = [] if amps_actual >= amps_max else ["force_amps"]
        return actions, True
    if force_started:
        # It ran and has stopped -- reaching the limit reports Complete.
        # Do NOT restart it: that would fight the owner's own stop forever.
        # force_expired() turns this into the expiry.
        return [], True
    return ["force_start", "force_amps"], False


def force_expired(*, force_charge_until: int | None, now: float,
                  car_charging: bool, force_started: bool) -> bool:
    """Whether the force should end now. Pure."""
    if not force_charge_until:
        return False
    if now >= force_charge_until:
        return True
    return bool(force_started) and not car_charging
```

Add to the imports at the top of `solar.py` if not already present:

```python
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
```

Check first — `solar.py` may already import some of these. Do not duplicate.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_charge_mode.py -v`
Expected: 20 passed

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: no regressions. `CONFIG_DEFAULTS` gained a key, so any test asserting an exact config key set will need updating — that is expected and correct.

- [ ] **Step 8: Commit**

```bash
git add solar.py tests/test_charge_mode.py
git commit -m "feat: charge_mode schema and the pure force-mode decisions

Mode is derived from force_charge_until plus enabled, never stored, so
enabled keeps its exact meaning and every existing consumer stays correct.

force_plan and force_expired are pure and tested without network, the same
shape as advance(). The force_started latch is what stops the mode expiring
on its own first tick -- right after charge_start the car reports Starting,
or briefly still Stopped."
```

---

## Task 4: `charge_mode` in the collector

**Files:**
- Modify: `collector.py:282` (the idle early-return), `collector.py:355-395` (the decide block), `collector.py:394-520` (the action loop), `collector.py:760` (constants), `collector.py:841` (loop locals), `collector.py:868` (`solar_wanted`), `collector.py:896-903` (`watching`), `collector.py:928-935` (the force wake), `collector.py:987-991` (`waiting_for_surplus`)
- Modify: `tests/test_collector_solar.py`

**Interfaces:**
- Consumes: `solar.forcing`, `solar.force_plan`, `solar.force_expired` (Task 3)
- Produces: `collector.FORCE_WAKE_MIN_S`. Otherwise wiring only.

### The five wiring points

- [ ] **Step 1: Write the failing test**

Append to `tests/test_collector_solar.py` (not `test_collector.py` — this file
already has the `Store` + fake-client harness this test needs):

```python
class _NightImportClient:
    """2 kW of import and no sun -- the exact condition that makes the solar
    machine stop a charge. A forced charge must survive it."""

    def __init__(self):
        self.commands = []

    async def _get(self, path, ttl=0):
        return {"grid_power": 2000.0, "solar_power": 0.0}

    async def command(self, vin, name, params):
        self.commands.append((name, dict(params)))
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

    client = _NightImportClient()
    cfg = SimpleNamespace(timezone="America/Denver",
                          proxy_url="https://localhost:4443")
    view = {
        "charging_state": "Charging", "amps_actual": 5, "amps_max": 48,
        "volts": 240, "charge_amps": 24, "soc": 55, "limit": 80,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }

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
```

Add `import tesla` and `from types import SimpleNamespace` to that file's
imports if not already present — both are, so no change should be needed.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_collector.py -k forced_charge -v`
Expected: FAIL — `charge_stop` appears in `commands`.

- [ ] **Step 3: Let the tick run while forcing (wiring point 1)**

In `collector.py`, replace line 282:

```python
    if not conf["enabled"] and st["state"] == "idle":
        return "idle", False
```

with:

```python
    now_ts = time.time()
    is_forcing = solar.forcing(conf, now_ts)

    # The cheap early-out, which force mode must not take: with enabled=0 and
    # the machine idle there is normally nothing to do, but a force is
    # precisely a reason to act with enabled=0.
    if not conf["enabled"] and not is_forcing and st["state"] == "idle":
        return "idle", False
```

- [ ] **Step 4: Branch the decision (wiring point 2)**

In `collector.py`, replace line 379:

```python
    machine, actions = solar.advance(machine, tick, solar.policy_from(conf), tun)
```

with:

```python
    if is_forcing:
        # The solar machine does not run at all while forcing -- it stays
        # idle, and force_plan decides. Note the tick is still LOGGED below:
        # green.charged_split derives the whole solar/grid attribution from
        # solar_ticks, and skipping the log would make a forced overnight
        # charge invisible to the ledger and to "miles added today".
        actions, started_next = solar.force_plan(
            state=st["state"], location=location, plugged=tick.plugged,
            car_charging=tick.car_charging,
            amps_actual=int(view.get("amps_actual") or 0),
            amps_max=tun.max_a, force_started=bool(st["force_started"]))
        machine = solar.Machine()
        if started_next != bool(st["force_started"]):
            solar.save_state(db, vin, force_started=int(started_next))
        if solar.force_expired(
                force_charge_until=conf["force_charge_until"], now=now_ts,
                car_charging=tick.car_charging,
                force_started=bool(st["force_started"])):
            _log("force charge expired; restoring and returning to solar")
            await _restore(client, db, vin, st, view)
            solar.save_state(db, vin, force_started=0)
            # solar_config is the WEB APP's table (see save_config's comment).
            # This is the one collector write to it, and it is deliberate:
            # nothing else can observe midnight. Keep it to this single field.
            solar.save_config(db, force_charge_until=None)
            actions = []
    else:
        machine, actions = solar.advance(machine, tick, solar.policy_from(conf), tun)
```

**Note the writer-separation exception.** `save_config` is documented as web-app-only. This is the single deliberate violation, confined to one field, and the comment above records why. Add a matching note to `solar.save_config`'s comment block.

- [ ] **Step 5: Handle the two new actions (wiring point 3)**

In `collector.py`'s action loop, add two branches alongside `charge_start` (line 425):

```python
        elif action == "force_start":
            # Record the restorable original BEFORE touching anything, the
            # same invariant charge_start holds at line 387: without a known
            # original there is nothing to restore to and both restore paths
            # silently no-op forever.
            if view.get("charge_amps") is None:
                _log("charge_amps unknown; refusing to force without a "
                     "restorable original")
                break
            if not st["dirty"]:
                solar.save_state(db, vin, dirty=1,
                                 original_amps=view.get("charge_amps"),
                                 engaged_at=int(now_ts))
            if not await _command(client, vin, "charge_start"):
                _log("forced charge_start refused")
                break

        elif action == "force_amps":
            await _command(client, vin, "set_charging_amps",
                           charging_amps=tun.max_a)
```

- [ ] **Step 6: Wire the three loop-level hooks (wiring point 4)**

In `collector.py`'s `run()`:

Line 868 — `solar_wanted` must include forcing, or the loop skips the tick entirely and nothing above ever runs:

```python
            solar_wanted = (bool(conf["enabled"]) or bool(st["dirty"])
                            or solar.forcing(conf, time.time()))
```

Line 897 — a forced charge must WAKE a sleeping car, not watch it. The meter-only watch deliberately makes no vehicle call, and force mode needs one:

```python
            if (car_state != "online" and solar_wanted
                    and not solar.forcing(conf, time.time())
                    and site_id is not None
                    and st["state"] in ("idle", "stopped")):
```

Line 987 — force mode is not waiting for surplus, and must not be stood down after dark, which is when a forced charge almost always runs:

```python
            waiting_for_surplus = bool(
                solar_wanted
                and not solar.forcing(conf, time.time())
                and (watching
                     or solar.load_state(store._db, vin)["state"]
                     in ("idle", "stopped")))
```

- [ ] **Step 7: Wake a sleeping car (wiring point 5)**

Disabling `watching` in Step 6 routes a sleeping car to `poll_once` — which
returns `view = None`, so the tick gate at line 944 skips the tick entirely
and the force never starts. Without this step, "charge my car up" issued at
2 a.m. fails **silently all night**.

Add a module constant next to `SITE_INGEST_INTERVAL_S` (line 760):

```python
# A wake is $0.02, 20x a command. A car that refuses to wake must not be
# asked every tick until midnight.
FORCE_WAKE_MIN_S = 300
```

Initialise a local next to `last_site_ingest` (line 841):

```python
    last_force_wake = 0.0
```

And insert this immediately after the `try/except TeslaAuthError` block that
sets `view` (after line 928, before the garage tick at line 935):

```python
            # A forced charge must WAKE a sleeping car. The meter-only watch
            # is disabled while forcing (above) precisely so we land here --
            # but poll_once returns no view for a sleeping car, and the tick
            # gate below would then skip the tick entirely.
            #
            # Rate-limited AND counted against the cap: this is the most
            # expensive request this system makes, and a car that will not
            # wake must not be asked every tick until midnight.
            if (solar.forcing(conf, time.time()) and view is None
                    and time.time() - last_force_wake >= FORCE_WAKE_MIN_S):
                last_force_wake = time.time()
                today = datetime.now(
                    ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")
                _, capped = solar.count_request(store._db, vin, today)
                if capped:
                    _log("force wake skipped: daily request cap reached")
                else:
                    _log("force charge: waking the car")
                    try:
                        await client.wake_up(vin)
                    except (TeslaAPIError, TeslaAuthError, httpx.HTTPError,
                            OSError) as exc:
                        _log(f"force wake failed: {exc}")
                    else:
                        car_state, view = await poll_once(
                            client, store, vin, settings)
```

`datetime` and `ZoneInfo` are already imported in `collector.py` (used at line
274). Verify before adding an import.

- [ ] **Step 8: Write the sleeping-car test**

Append to `tests/test_collector_solar.py`:

This one drives the real `run()` loop, so it uses the `_FakeLoopClient` /
`StopTest` pattern already in that file rather than `solar_tick` directly:

```python
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
```

The five iterations complete in microseconds of wall-clock, so they all fall
inside one `FORCE_WAKE_MIN_S` window — that is what makes the count assertion
meaningful rather than timing-dependent.

- [ ] **Step 9: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_collector_solar.py -k "forced_charge or wakes_a_sleeping" -v`
Expected: PASS

- [ ] **Step 10: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: no regressions.

- [ ] **Step 11: Commit**

```bash
git add collector.py solar.py tests/test_collector_solar.py
git commit -m "feat: charge_mode now, honoured by the collector

Five wiring points: the idle early-out, the decision branch, two new
actions, three loop-level hooks, and a wake for a sleeping car. The solar
machine does not run at all while forcing -- force_plan decides and the
machine stays idle.

The wake matters more than it looks: disabling the meter-only watch while
forcing routes a sleeping car to poll_once, which returns no view, and the
tick gate would then skip the tick entirely -- so a charge forced at 2 a.m.
would fail silently all night.

The tick is still logged while forcing. green.charged_split derives the
whole solar/grid attribution from solar_ticks, so skipping the meter read
would make a forced overnight charge invisible to the ledger and to 'miles
added today' -- far worse than the downward bias already documented."
```

---

## Task 5: `GET`/`PUT /api/car/charge-mode`

**Files:**
- Modify: `solar_routes.py:327-354`
- Modify: `tests/test_solar_routes.py`

**Interfaces:**
- Consumes: `solar.charge_mode`, `solar.next_midnight_ts`, `solar.forcing` (Task 3)
- Produces: `GET /api/car/charge-mode` → `{"mode": str, "expires_ts": int|None, "enabled": bool}`; `PUT /api/car/charge-mode` body `{"mode": "solar"|"now"|"off"}` → the same shape

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_solar_routes.py`:

These use the file's existing `client` fixture — a bare `FastAPI` with only
`solar_routes.router`, a fresh `tmp_path` DB, and `solar_routes._store` reset.
Note `monkeypatch.setattr(solar_routes, "DEMO", False)`: the module defaults to
demo mode under test, and these need the real store-backed path.

```python
import time

import solar


def test_charge_mode_starts_at_off(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    body = client.get("/api/car/charge-mode").json()
    assert body["mode"] == "off"
    assert body["expires_ts"] is None


def test_setting_now_stamps_the_next_midnight(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    body = client.put("/api/car/charge-mode", json={"mode": "now"}).json()
    assert body["mode"] == "now"

    from config import settings
    assert body["expires_ts"] == solar.next_midnight_ts(
        settings.timezone, time.time())
    assert solar.load_config(solar_routes.store()._db)[
        "force_charge_until"] == body["expires_ts"]


def test_setting_solar_clears_the_force_and_enables(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.put("/api/car/charge-mode", json={"mode": "now"})
    body = client.put("/api/car/charge-mode", json={"mode": "solar"}).json()

    assert body["mode"] == "solar"
    assert body["expires_ts"] is None
    conf = solar.load_config(solar_routes.store()._db)
    assert conf["force_charge_until"] is None
    assert conf["enabled"] == 1


def test_setting_off_clears_the_force_and_disables(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    client.put("/api/car/charge-mode", json={"mode": "now"})
    body = client.put("/api/car/charge-mode", json={"mode": "off"}).json()

    assert body["mode"] == "off"
    conf = solar.load_config(solar_routes.store()._db)
    assert conf["force_charge_until"] is None
    assert conf["enabled"] == 0


def test_an_unknown_mode_is_refused(client, monkeypatch):
    monkeypatch.setattr(solar_routes, "DEMO", False)
    assert client.put(
        "/api/car/charge-mode", json={"mode": "fast"}).status_code == 400
    assert client.put("/api/car/charge-mode", json={}).status_code == 400


def test_force_charge_until_is_not_writable_through_the_config_route(
        client, monkeypatch):
    """A client that could set the timestamp directly could set it a year
    out, and the midnight expiry -- the whole safety property of "now" --
    would be gone. The mode route is the only way in."""
    monkeypatch.setattr(solar_routes, "DEMO", False)
    r = client.put("/api/car/solar/config",
                   json={"force_charge_until": 99999999999})
    assert r.status_code == 400
    assert "unknown fields" in r.json()["detail"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_solar_routes.py -k charge_mode -v`
Expected: FAIL — `AttributeError: module 'solar_routes' has no attribute 'put_charge_mode'`

- [ ] **Step 3: Add the routes**

In `solar_routes.py`, after `put_solar_config` (line 353):

```python
CHARGE_MODES = ("solar", "now", "off")


def _mode_payload(conf: dict, now: float) -> dict[str, Any]:
    return {
        "mode": solar.charge_mode(conf, now),
        # Only meaningful while forcing. Reported as null otherwise rather
        # than as a stale timestamp the caller has to interpret.
        "expires_ts": (conf["force_charge_until"]
                       if solar.forcing(conf, now) else None),
        "enabled": bool(conf["enabled"]),
    }


@router.get("/charge-mode")
async def get_charge_mode() -> dict[str, Any]:
    return _mode_payload(solar.load_config(store()._db), time.time())


@router.put("/charge-mode")
async def put_charge_mode(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Set the charge mode.

    force_charge_until is deliberately NOT in CONFIG_BOUNDS, so it cannot be
    written through PUT /solar/config. A client that could set the timestamp
    directly could set it a year out, and the midnight expiry -- the whole
    safety property of "now" -- would be gone. This route is the only way in,
    and it computes the expiry itself.
    """
    mode = body.get("mode")
    if mode not in CHARGE_MODES:
        raise HTTPException(400, f"mode must be one of {list(CHARGE_MODES)}")

    now = time.time()
    if mode == "now":
        solar.save_config(
            store()._db,
            force_charge_until=solar.next_midnight_ts(settings.timezone, now))
    elif mode == "solar":
        solar.save_config(store()._db, force_charge_until=None, enabled=1)
    else:
        solar.save_config(store()._db, force_charge_until=None, enabled=0)

    return _mode_payload(solar.load_config(store()._db), now)
```

`force_charge_until` must **not** be added to `CONFIG_BOUNDS` — the test above asserts that.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_solar_routes.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add solar_routes.py tests/test_solar_routes.py
git commit -m "feat: GET/PUT /api/car/charge-mode

force_charge_until stays out of CONFIG_BOUNDS deliberately: a client that
could write the timestamp could set it a year out, and the midnight expiry
is the whole safety property of 'now'. This route computes the expiry
itself and is the only way in."
```

---

## Task 6: `mcp_server.py` and the seven tools

**Files:**
- Create: `mcp_server.py`
- Create: `tests/test_mcp_server.py`
- Modify: `app.py` (mount above the static catch-all; compose the lifespan)
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `ha_routes.ha_state()`, `ha_routes.store()`, `solar.charge_mode`, `solar_routes.put_charge_mode`, `solar_routes.get_garage`, `car_routes.car_wake`, `green.charged_split`, `green.miles_per_kwh`
- Produces: `mcp_server.mcp` (the FastMCP instance), `mcp_server.READ_TOOLS` (tuple of names, used by the cost test)

- [ ] **Step 1: Add the dependency**

Append to `requirements.txt`:

```
mcp>=1.2
```

Run: `.venv/bin/pip install -r requirements.txt`

- [ ] **Step 2: Write the failing tests**

Create `tests/test_mcp_server.py`:

```python
from __future__ import annotations

import pytest

import mcp_server


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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_server'`

- [ ] **Step 4: Write `mcp_server.py`**

Structure to follow — write each tool as a thin wrapper, and register both the
FastMCP tool and an entry in `CALLABLES` so the tests can call them directly
without an MCP session:

```python
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
```

Then:

```python
from __future__ import annotations

import time
from typing import Any

from mcp.server.fastmcp import FastMCP

import car_routes
import green
import ha_routes
import solar
import solar_routes
from config import settings

mcp = FastMCP("tesla")

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
```

Each tool follows this shape — register in `CALLABLES` and decorate with
`@mcp.tool()`:

```python
@mcp.tool()
async def get_charging_summary(period: str = "today") -> dict[str, Any]:
    """How much energy and how many miles went into the car, split by source.

    `period` is "today" (calendar day), "week" (rolling 7 days) or "all".
    """
    if period not in PERIODS:
        raise ValueError(f"period must be one of {sorted(PERIODS)}")
    label, since_of = PERIODS[period]
    now = time.time()
    db = solar_routes.store()._db
    vin = solar_routes._vin()

    ticks = solar_routes._solar_ticks(db, vin, since_of(now))
    solar_kwh, grid_kwh = green.charged_split(ticks)

    sessions, segments = solar_routes._sessions_and_segments(db, vin)
    pack, _ = green.pack_kwh(sessions)
    mpk, sampled = green.miles_per_kwh(segments, pack)

    conf = solar.load_config(db)
    total = solar_kwh + grid_kwh
    return {
        "window": label,
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
        "miles_unknown_reason": None if mpk else (
            f"only {sampled:.0f} miles sampled so far; need more driving "
            "history before energy can be converted to miles"),
        "charge_mode": solar.charge_mode(conf, now),
    }


CALLABLES["get_charging_summary"] = get_charging_summary
```

The remaining six:

```python
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
async def get_solar_status() -> dict[str, Any]:
    """Live power at the house right now, and what the charge controller is
    doing about it."""
    st = await ha_routes.ha_state()
    return {
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -v`
Expected: PASS

- [ ] **Step 6: Mount it into the app**

In `app.py`, add `import mcp_server` to the import block, and mount **above** the `app.mount("/", _NoStoreStatic(...))` line (line 289) — below it the route is unreachable:

```python
app.mount("/mcp", mcp_server.mcp.streamable_http_app())
```

The session manager needs its lifespan composed into the app's, or every request fails with "session manager not initialized". Change the existing `lifespan` (line 59):

```python
@asynccontextmanager
async def lifespan(app_: FastAPI):
    # FastMCP's streamable-HTTP transport keeps per-session state that is
    # created by ITS lifespan. Mounting the sub-app does not run it -- the
    # parent owns startup -- so it must be entered here or every /mcp request
    # fails with "session manager not initialized".
    async with mcp_server.mcp.session_manager.run():
        yield
    await client.aclose()
```

**Verify the exact attribute name against the installed SDK** before writing this — run `.venv/bin/python -c "from mcp.server.fastmcp import FastMCP; print([a for a in dir(FastMCP) if 'session' in a or 'http' in a])"`. The SDK's API has moved between versions; use what the installed version exposes and adjust the two lines above to match.

- [ ] **Step 7: Verify it serves end to end**

```bash
API_TOKEN_MCP=testtoken .venv/bin/python app.py &
sleep 3
curl -sS -X POST http://127.0.0.1:8000/mcp \
  -H 'X-Api-Key: testtoken' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -40
```

Expected: a JSON-RPC result listing all seven tools. Then confirm the guard:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/mcp -d '{}'
```

Expected: `401`.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py app.py requirements.txt
git commit -m "feat: MCP server with seven tools

Reads resolve from SQLite; the cost guarantee is re-cut per tool because
ha_routes' import ban cannot apply here -- set_charge_mode and wake_car
legitimately need the client. Each read runs against a client that raises
on any attribute access.

No open_garage, no confirm=True commands, no tunable writes: what is absent
is as much of the design as what is here."
```

---

## Task 7: Force-mode ledger and backtest coverage

**Files:**
- Modify: `tests/test_collector_solar.py`
- Modify: `tests/test_charge_mode.py`

**Interfaces:**
- Consumes: everything from Tasks 3 and 4

**Not `tools/backtest.py`.** The spec named it, and reading it shows it is the
wrong home: it replays *real Tesla calendar_history for a specific past day*
over the network, and only three days on this account are valid inputs (days
the car was away, or its own draw double-counts). There is no scenario format
to add a synthetic force run to. The lifecycle coverage the spec wanted is
Step 3 below, as a pure sequence test — no network, and it actually runs in CI.

- [ ] **Step 1: Write the ledger-continuity test**

Append to `tests/test_collector_solar.py`:

```python
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

    client = _NightImportClient()      # 2 kW import, zero sun
    cfg = SimpleNamespace(timezone="America/Denver",
                          proxy_url="https://localhost:4443")
    view = {
        "charging_state": "Charging", "amps_actual": 48, "amps_max": 48,
        "volts": 240, "charge_amps": 24, "soc": 55, "limit": 80,
        "lat": 40.0, "lon": -105.0,
        "fast_charger_present": False, "fast_charger": None,
    }

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
```

Add `import green` to that file's imports.

- [ ] **Step 2: Run it to verify it fails, then passes**

Run: `.venv/bin/python -m pytest tests/test_collector_solar.py -k attribution_ledger -v`

If it fails on the row count, the bug is real — Task 4's force branch returned
before `solar.log_tick(...)`. Fix `collector.py` so the log call is reached on
the force path too, then re-run.

- [ ] **Step 3: Write the full-lifecycle sequence test**

Append to `tests/test_charge_mode.py`:

```python
def test_the_whole_force_lifecycle_in_sequence():
    """Solar engaged -> force set -> hand-off -> start -> latch -> Complete
    -> expiry. Pure, so it runs in CI with no network and no Tesla account.

    This is the coverage the spec wanted from a backtest scenario.
    tools/backtest.py cannot host it: that harness replays real
    calendar_history for one of three specific past days over the network.
    """
    until = 5000
    started = False
    issued = []

    def plan(state, car_charging, amps_actual):
        nonlocal started
        actions, started = solar.force_plan(
            state=state, location="home", plugged=True,
            car_charging=car_charging, amps_actual=amps_actual,
            amps_max=48, force_started=started)
        issued.append(actions)
        return actions

    # 1. The solar controller is mid-engagement at its 5 A floor.
    assert plan("charging", True, 5) == ["restore"]

    # 2. Handed off -- the machine is idle, the car has stopped.
    assert plan("idle", False, 0) == ["force_start", "force_amps"]
    assert started is False, "the latch must not set before charging is seen"

    # 3. Charging observed at the floor -- lift it, and latch.
    assert plan("idle", True, 5) == ["force_amps"]
    assert started is True

    # 4. Steady at the target -- no commands at all.
    assert plan("idle", True, 48) == []

    # 5. The car reached its charge limit and stopped.
    assert plan("idle", False, 0) == []
    assert solar.force_expired(force_charge_until=until, now=1000,
                               car_charging=False, force_started=True)

    # And nothing ever tried to restart it after the latch was set.
    assert issued.count(["force_start", "force_amps"]) == 1, (
        f"restarted a finished charge: {issued}")


def test_midnight_expires_a_charge_that_is_still_running():
    """The other expiry path. The owner chose midnight, so a charge still in
    progress at 00:00 is stopped and handed back to solar."""
    assert solar.force_expired(force_charge_until=5000, now=5000,
                               car_charging=True, force_started=True)
```

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_collector_solar.py tests/test_charge_mode.py
git commit -m "test: force mode keeps the attribution ledger honest

A forced charge that logged no ticks would be invisible to
green.charged_split -- 'miles added today' would under-report by the whole
charge and the Energy Dashboard car meters would flatline through it.

The lifecycle coverage is a pure sequence test rather than a backtest
scenario: tools/backtest.py replays real calendar_history for one of three
specific past days over the network and has no way to host a synthetic run."
```

---

## Verification: the whole feature, end to end

After Task 7, before calling this done:

- [ ] `.venv/bin/python -m pytest -q` — all pass, no skips that were not skipped before
- [ ] `curl -H 'X-Api-Key: …' -X PUT http://127.0.0.1:8000/api/car/charge-mode -d '{"mode":"now"}' -H 'Content-Type: application/json'` returns `{"mode":"now","expires_ts":…}` and the timestamp is tonight's midnight in `TESLA_TIMEZONE`
- [ ] `sqlite3 car.db 'SELECT force_charge_until FROM solar_config'` shows that same value
- [ ] `.venv/bin/python collector.py --once` with the force live issues `charge_start`, not `charge_stop`
- [ ] `sqlite3 car.db 'SELECT COUNT(*) FROM solar_ticks WHERE ts > <force_set_ts>'` is non-zero — the ledger saw the forced charge
- [ ] Set `force_charge_until` to a past timestamp by hand, run `collector.py --once`, and confirm the column returns to NULL and a restore was issued
- [ ] From another LAN host: `curl http://192.168.87.84:8000/api/car/home` → **401**
- [ ] `tools/list` over `/mcp` shows exactly seven tools

**Known-incomplete on purpose:** `close_garage` returns a refusal until spec
task 9 builds `POST /api/car/garage/close_unattended`, which is blocked on the
macOS Local Network grant. That is the correct behaviour for now — a tool that
silently called the blunt `close` would be the exact failure `garage.py`'s
docstring warns about.
