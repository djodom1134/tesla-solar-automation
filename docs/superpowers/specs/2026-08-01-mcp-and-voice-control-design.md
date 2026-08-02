# MCP server and voice control — Design

**Date:** 2026-08-01
**Status:** design, not yet implemented.
**Companion to:** `2026-08-01-home-assistant-integration.md`, which this
extends rather than replaces. Everything that spec says about the cost rule,
the entity table and the Energy Dashboard still holds.

## The request, and the correction it needs

The ask was: *"create an MCP server for this project so that I can use HA and
Google Home to ask 'hey google, charge my car up', 'charge the car with just
the sun', 'make sure the garage doors are closed', 'how many miles have been
added to my car today?'"*

**MCP is not in the "Hey Google" path, at any point.** A spoken command goes
Google Assistant → Google's smart-home cloud → Nabu Casa or a self-hosted
Google Smart Home Action → HA *entities*. Google matches on device name and
trait; it never forwards a free-form sentence to Home Assistant. MCP is a
protocol for LLM clients. HA does ship an `mcp` integration, but it feeds
HA's own Assist conversation agent — reachable from the HA app or a Voice PE
puck, not from a Google Home speaker.

So the request is two projects, and this document specifies both:

| Phrase | Delivered by |
|---|---|
| "charge my car up" | Google Action → HA script → `charge_mode=now` |
| "charge the car with just the sun" | Google Action → HA script → `charge_mode=solar` |
| "make sure the garage doors are closed" | Google Action → HA `cover` (blocked on the Local Network grant) |
| "how many miles have been added today?" | **MCP only.** Google's smart-home protocol has no trait for a generic numeric sensor. |

The owner chose to build both, MCP first.

## Verified before writing this

| Claim | How verified | Result |
|---|---|---|
| A forced charge is killed by the control loop | read `solar.py:309-331` | `idle` + `car_charging` → `adopt` → `set_amps` at negative surplus → 2 breach ticks → `grace` → budget expiry → `charge_stop`. Confirmed |
| `ha.darrenodom.com` is publicly reachable with a valid cert | `curl -w '%{http_code} %{ssl_verify_result}'` | `200`, `ssl_verify=0`, 59 ms. The hard prerequisite for a self-hosted Google Action is already met |
| HA is up on the LAN | `curl http://192.168.87.6:8123/` | `403` — HA's trusted-proxy rule, not a failure |
| `auth.py` does not exist | `ls` | absent. HA spec Task 1 never shipped |
| `API_TOKEN` / `TESLA_VIN` are unset | read `.env` | both absent |
| `car_command` spends outside the daily cap | read `car_routes.py:170-216` | no `_spend()` call. `car_wake` (`:159`) does have one |
| The collector's tick gate | read `collector.py:868, 944, 999` | `solar_wanted`, the tick gate, and the dark stand-down are the three hooks force mode needs |
| MCP SDK is not a dependency | read `requirements.txt` | FastAPI, uvicorn, httpx, dotenv, pytest only. Python 3.13.7 |

## Assumption on record

**One garage door.** The request said "doors"; the codebase knows exactly one
ratgdo at `192.168.87.78`. Everything here is designed for one. A second
opener means a second `garage_url` and a per-door latch, and is out of scope.

---

# 1. ARCHITECTURE

```
Claude (Code / Desktop / phone)
        │  MCP over streamable HTTP, X-Api-Key
        ▼
┌───────────────────────────────────────────────┐
│ FastAPI app on the mini :8000  (one process)  │
│                                               │
│  auth.py ──── token + CSRF middleware  [NEW]  │
│                                               │
│  /mcp        mcp_server.py             [NEW]  │
│  /api/ha/*   ha_routes.py       (SQLite only) │
│  /api/car/*  car_routes.py      (billed)      │
│  /api/car/solar/*, /garage/*  solar_routes.py │
└───────────────────────────────────────────────┘
        │                          │
        ▼                          ▼
   car.db (SQLite)          Tesla Fleet API
                            ratgdo @ .78
```

`mcp_server.py` sits beside `ha_routes.py` and calls **Python functions
directly**, not its own HTTP. Reads use the SQLite-only helpers `ha_routes`
already uses; writes go through `car_routes`, where `solar.count_request()`
and the daily cap live. Nothing is reimplemented, so there is no second copy
of the cost rule to drift out of sync.

### Rejected alternatives

- **Standalone stdio MCP server.** Simplest possible — no transport, no auth,
  no exposure — but it works only on a machine that can both run the process
  and reach the mini. No phone, and HA's `mcp` integration speaks HTTP/SSE and
  could never consume it. It would be rewritten as the above the first time
  either was wanted.
- **HA's built-in `mcp_server` integration instead of writing one.** Zero code,
  but Assist exposes entity states and intents only. It would surface
  `sensor.tesla_battery` and not the solar/grid attribution split, the banked
  ledger, free miles driven, or tick history — which is most of what makes
  "how many miles did the sun add today" answerable. It also inverts the
  dependency: car data would reach Claude only by first becoming an HA entity.
- **MCP tools calling our own HTTP endpoints over loopback.** Preserves the
  auth middleware for free, but adds a serialisation round trip inside one
  process to re-derive data the function call already has.

### Three implementation notes that cost time if unstated

1. **Mount above the static catch-all.** `app.py:288` — any router included
   after the `StaticFiles` mount at `/` is unreachable. `/mcp` goes next to
   `ha_routes`.
2. **FastMCP's session manager lifespan must be composed into the app's.** The
   app already has a `lifespan` (it closes the Tesla client). Forget this and
   every request fails with "session manager not initialized".
3. **The `ha_routes` import ban cannot apply wholesale.** That module's
   guarantee is "never imports the Tesla client", enforced by a test.
   `mcp_server` legitimately needs the client for `set_charge_mode` and
   `wake_car`. The invariant is therefore re-cut per tool: **every read tool is
   asserted to touch no Tesla client**, tested with a mock that fails the test
   if called.

New dependency: `mcp` (the official Python SDK).

---

# 2. THE TOOL SURFACE

Seven tools, shaped as questions and intents rather than one per endpoint.
Everything above the line is free — pure SQLite, no Fleet API — so an LLM that
polls costs nothing.

| Tool | Returns / does | Cost |
|---|---|---|
| `get_car_status` | SoC, charge limit, range, odometer, plugged, charging state, home/away/unknown, **data age** | free |
| `get_charging_summary(period)` | kWh and miles added split solar vs grid, solar share, current charge mode. `today` \| `week` \| `all` | free |
| `get_solar_status` | live solar / grid / house / car power, surplus, controller state, amps, flags (`capped`, `rate_limited`, `dirty`, `ledger_stale`) | free |
| `get_garage_status` | door state, obstruction, reachable | free (LAN) |
| `set_charge_mode(mode)` | `solar` \| `now` \| `off`. Returns the resulting mode and expiry | billed **only when it commands** — see below |
| `close_garage` | the unattended-close discipline, never the blunt close. Returns the door state after the attempt | LAN |
| `wake_car` | explicit wake, rate-limited | billed, capped |

### Exactly when `set_charge_mode` spends

`solar` and `off` write config only and are free. `now` is billed: it may issue
`charge_start` and `set_charging_amps`, and — if the machine was not `idle` —
a `restore` first. Leaving `now` (by expiry or by an explicit `solar`/`off`)
spends the restore. All of it goes through `car_routes`, so all of it is
counted against `daily_request_cap` once HA spec Task 2 lands.

**`set_charge_mode("now")` on a sleeping car issues a wake.** That is the most
expensive request this system makes ($0.02 against a $10/month credit), and it
is spent because the owner asked for a charge out loud — the same consent
standard as the `wake_car` tool. It is counted and capped like any other. The
alternative, silently doing nothing until the car happens to wake, would make
"charge my car up" fail invisibly overnight.

### Period semantics

`_solar_ticks(db, vin, since_ts)` already carries the convention (`0` for all
time, `_midnight_ts()` for today). The tool's three values map onto it:

| `period` | `since_ts` |
|---|---|
| `today` | local midnight in `settings.timezone` — **not** a rolling 24 h |
| `week` | rolling 7 × 86400 s, matching `car_routes.RANGES["7d"]` — **not** a calendar week |
| `all` | `0`, lifetime |

`today` and `week` are deliberately inconsistent with each other because each
matches the question it answers: "today" is the calendar day the owner is
living in, "this week" is a trend and a rolling window is the more useful one.
The tool response names the window it used so the model never has to guess.

### How `get_charging_summary("today")` is computed

The pieces exist and compose; nothing new is measured.

```
ticks   = _solar_ticks(db, vin, _midnight_ts())      # solar_routes.py:55-73
kwh     = green.charged_split(ticks)                 # -> (solar_kwh, grid_kwh)
mpk, _  = green.miles_per_kwh(segments, pack_kwh)    # MEASURED, not rated
miles   = kwh * mpk
```

`green.miles_per_kwh` is the owner's own measured consumption, not the car's
rated-range guess. When it has not cleared its sample threshold the tool
returns `miles: null` with `basis: null` and a reason string — never a figure
resting on `NOMINAL_PACK_KWH`, which the existing spec records as ~30% wrong
on this car. The kWh figures are always available; only the miles conversion
can be unknown.

### Deliberately not exposed

Extending the reasoning of the HA spec §2, with one addition.

- **`open_garage`.** Closing was requested; opening was not. An LLM tool that
  opens the garage is a physical-security exposure reachable by prompt
  injection, and the arrival automation in `collector.py` already covers the
  legitimate case. **This is the one omission most likely to be wanted later**
  — it is a decision, not an oversight.
- **Every `commands.py` entry with `confirm=True`** — `door_unlock`,
  `schedule_software_update`, `speed_limit_set_limit` among them. `confirm` is
  the human-in-the-loop marker; `risk` is only a display hint.
- **`trigger_homelink`.** A blind stateless toggle. A second way to move the
  same door is how a system ends up believing a door is closed when it is not.
- **The tunables** (`margin_w`, `min_a`, `soc_ceiling`, `grace_budget_wh`). HA
  will have bounded sliders; there is no voice or chat case for "set my export
  margin to 250 watts". A short tool list is also what makes a model pick the
  right tool.
- **`garage_url`, home lat/lon/radius.** SSRF primitive, and retroactive
  redefinition of what counted as a home charge. Setup page only.

---

# 3. `charge_mode` IN THE COLLECTOR

The load-bearing part. Everything else is plumbing on top of it.

## Why a wrapper endpoint cannot work

`solar.py:309-331`: with `enabled=1`, a charge started from outside lands in
`idle` with `car_charging=True`, is **adopted**, and `set_amps` is written
against a negative night-time surplus. Two ticks of `floor_breach` move it to
`grace`; the grace energy budget expires; `charge_stop` and `restore` fire.
Roughly four billed commands to end exactly where it began. The HA spec
already recorded this and prescribed the fix: a mode selector implemented
*inside* the collector, with a midnight expiry.

## Mode is derived, not a fourth source of truth

`enabled` keeps its exact current meaning, so every existing test, the HA
switch and the setup page stay correct. One new config column carries the
override:

```
force_charge_until  INTEGER   -- unix ts; NULL = not forcing       [solar_config]
force_started       INTEGER   -- have we yet observed it charging? [solar_state]

mode = "now"   if force_charge_until and now < force_charge_until
       "solar" if enabled
       "off"
```

The split follows the precedent already in the schema: *intent* lives in
`solar_config` (like `garage_close_hour`), the per-VIN *latch* lives in
`solar_state` (like `garage_armed`).

| `set_charge_mode(...)` | Writes |
|---|---|
| `now` | `force_charge_until = next local midnight`, `force_started = 0` |
| `solar` | `force_charge_until = NULL`, `enabled = 1` |
| `off` | `force_charge_until = NULL`, `enabled = 0` |

`now` leaves the **charge limit unchanged** — whatever the owner set. Raising
it to `soc_ceiling` was considered and rejected: it spends extra commands and
drives the existing `raise_limit` machinery from a second trigger.

## The three hooks in `collector.py`

1. **`solar_wanted` (`:868`)** becomes `enabled or dirty or forcing`. Without
   this the loop skips the tick entirely and nothing below ever runs.
2. **Before `advance()` (`:379`):** when forcing, do not call it at all.
   Instead — if the machine is not `idle`, run `_restore()` and reset it to
   `idle` first, so the controller is not left holding the car at its 5 A floor
   with unsettled `dirty` / `original_amps` bookkeeping. Then command
   `charge_start` + `set_charging_amps(amps_max)` **only when the observed
   state differs from the intent**, reusing the existing "already holds this
   value" suppression. Steady state is zero commands per tick.
3. **`waiting_for_surplus` (`:999`)** is forced False so the after-dark
   stand-down does not fire. A forced charge is usually at night, which is
   exactly when that stand-down would kill it.

## Expiry

Clears `force_charge_until`, runs the existing `may_restore()` path to put back
the owner's standing amps, and drops back to `solar`. Two triggers:

- local midnight passes, **or**
- `charging_state` leaves `{Charging, Starting}` **and** `force_started` is set.

The latch is load-bearing. Immediately after `charge_start` the car reports
`Starting`, or briefly still `Stopped`; without the latch the mode would expire
on its own first tick. `force_started` is set the first tick charging is
actually observed. This also makes `Complete` — reaching the charge limit — a
clean revert, which is the success case.

## Force mode costs the same per tick as solar mode

The intuition runs the other way: there is no control loop to run while
forcing, so why read the meter? Because `green.charged_split()` derives the
entire solar/grid attribution from `solar_ticks`, and a tick row needs
`car_w` **and** `grid_w`. Skip `live_status` and a forced overnight charge
becomes **invisible to the ledger**: `get_charging_summary("today")`
under-reports by the whole charge, and the Energy Dashboard's car meters
flatline through it. That is far worse than the 5–15% downward bias the HA
spec already documents.

Booking it all to grid without reading the meter would be a fair guess at
2 a.m. and simply wrong for "charge my car up" on a cloudy afternoon. So the
meter read stays and the saving is not available.

## Two accepted failure modes

- A charge forced at 23:50 reverts to solar ten minutes later. Correct per the
  chosen semantics, and it will look abrupt the first time. A rolling N-hour
  window was rejected because it does not expire predictably.
- If the app is down at midnight, expiry happens on the first tick after it
  returns, not at midnight. `force_charge_until` is a timestamp compared
  against `now`, so it is late but never missed — a flag plus a day-stamp would
  have been lost outright.

---

# 4. AUTH AND BLAST RADIUS

## Prerequisites, both already specified and neither shipped

- **HA spec Task 1 — `auth.py`.** Non-loopback requests require `X-Api-Key`;
  every mutating method rejects a cross-site `Sec-Fetch-Site`. The loopback
  exemption alone does not close drive-by CSRF, because that form targets
  `127.0.0.1:8000` from the owner's own browser. `/api/ha/*` and `/mcp` require
  the header **even from loopback** — they are control surfaces, not the local
  UI.
- **HA spec Task 2 remainder.** `TESLA_VIN` is unset, so `car_routes._vin()`
  falls through to a billed `GET /api/1/vehicles` on every call. And
  `car_command` has no `_spend()`, so `POST /api/car/command/{id}` still spends
  outside the daily cap. `set_charge_mode` issues commands through that path,
  so the cap must be real before an LLM can drive it.

`API_TOKEN_MCP` is a third token, rotatable independently of the browser UI's
and HA's — the same reasoning as the existing `API_TOKEN` / `API_TOKEN_HA`
split.

## The safety comes from the tool surface, not the client's confirm dialog

Claude Code and Desktop prompt per tool call, but that is a property of those
clients, not of this server. The real bound is what the tools cannot do: no
`open_garage`, no `door_unlock`, no `confirm=True` commands, no `garage_url`,
no home coordinates.

Worst realistic outcome from a hostile prompt is `set_charge_mode("now")`: one
night of grid charging, ~70 kWh at $0.12 ≈ **$8**, self-reverting at midnight
and plainly visible in the ledger the next morning. Bounded, reversible,
observable.

## The garage close is the irreversible direction

`garage.py`'s module docstring is explicit that automation only ever *opens*,
and that closing happens on a schedule with a warning sequence, never
triggered by the car's position. `close_garage` therefore routes to the
**unattended** path specified as HA spec Task 9 — `safe_to_close()` gate, light
on, `garage_close_warn_s`, re-read, abort on obstruction or on the door no
longer being `Open` — and **never** to `POST /api/car/garage/close`, which is
documented as "the owner is present and just pressed it." An MCP call is not a
person standing there.

This whole path remains blocked on the macOS Local Network grant. Until that is
done the tool returns `unreachable`.

---

# 5. THE GOOGLE LAYER (PHASE 2)

No Nabu Casa, so this is HA's manual `google_assistant` integration: a Google
Cloud project, a smart-home Action with fulfilment at
`https://ha.darrenodom.com/api/google_assistant`, OAuth account linking, and a
service account key for Report State and `requestSync`. Public HTTPS with a
valid cert is the hard prerequisite and is already satisfied. The remainder is
console work, not code.

| Phrase | Exposed as | Works |
|---|---|---|
| "activate charge my car" | `script` → Google Scene → `charge_mode=now` | yes |
| "activate solar charging" | `script` → Google Scene → `charge_mode=solar` | yes |
| "turn on solar charging" | the `switch` from HA spec §2 Group A | yes |
| "close the garage" / "is the garage open?" | `cover`, `device_class: garage` | yes, once the Local Network grant is done |
| "how many miles added today?" | — | **no** — no trait for a generic numeric sensor |

Two expectations to set. Google matches on **device name plus trait**, so the
phrasing is "activate charge my car", not free-form — not every phrase in the
original request works verbatim. And Google requires `secure_devices_pin` to
*open* a garage cover; closing does not need it.

The miles question has one workaround, documented but **not built**: a Google
Routine with a custom phrase activating a scene → HA script → `tts.speak` to a
Google speaker. It works, and it is four moving parts to answer one question
Claude answers directly. Ship the MCP tool first and see whether it is missed.

---

# 6. FAILURE BEHAVIOUR AND TESTING

Every read tool returns its **age** alongside its value and says so when
stale, rather than presenting a stale number as current — the project's
existing rule, carried into the tool contract.

| Condition | Tool returns |
|---|---|
| Collector dead, app alive | `collector_running: false`; values reported as unverifiable, not as live |
| Car asleep | snapshot values with `age_s`, explicitly labelled |
| ratgdo unreachable | `reachable: false` — **never** `closed` |
| Tesla 429 | `rate_limited: true`, with the backoff still in force |
| Daily cap tripped | write tools refuse with the count and the reset time |
| `mi_per_kwh` below threshold | kWh figures with `miles: null`, `basis: null`, and a reason |
| DEMO mode | write tools refuse; read tools label the data synthetic |

## Tests

- **Per-tool cost assertion.** Each read tool runs against a Tesla client mock
  that fails the test if called. This replaces `ha_routes`'s module-level
  import ban, which cannot apply here.
- **`charge_mode` machine:** expiry at midnight; expiry on `Complete`; the
  `force_started` latch not expiring on the first tick; `_restore()` on entry
  when the machine is not `idle`; `solar_wanted` and the dark stand-down both
  honouring force.
- **Ledger continuity:** a forced overnight charge produces `solar_ticks` rows
  and moves `charged_grid_kwh`. This is the regression that would silently
  break "miles added today".
- **Auth:** per HA spec Task 1 — 401 from another LAN host, 403 on cross-site
  `Sec-Fetch-Site`, `/mcp` refusing an absent header from loopback.
- `tools/backtest.py` gains a force-mode scenario, so the whole loop is
  exercised with no network.

---

# 7. TASKS

Each step is useful on its own. The Google work is last because it is the only
part that is mostly not code.

**Tasks 1–7 are the implementation plan's scope** — all code in this repo,
ending with a working MCP server and a `charge_mode` the collector honours.
**Tasks 8–10 are follow-on configuration** on servy and in Google's console,
and get their own plan once 1–7 are shipped and verified. Splitting there is
deliberate: 1–7 can be tested locally with `pytest` and the backtest harness,
while 8–10 cannot be verified without touching two systems outside this repo.

| # | Task | Files |
|---|---|---|
| 1 | `auth.py` — token + CSRF middleware (HA spec Task 1) | new `auth.py`, `app.py`, `.env`, `deploy-macos.sh` |
| 2 | Close the cost leaks: set `TESLA_VIN`; `_spend()` in `car_command`; 60 s rate limit on `/wake` | `.env`, `car_routes.py` |
| 3 | `force_charge_until` + `force_started` schema and migration | `solar.py`, `store.py` |
| 4 | `charge_mode` in the collector — the three hooks and expiry | `collector.py`, `solar.py` |
| 5 | `GET`/`PUT` charge mode on the HTTP API | `solar_routes.py` |
| 6 | `mcp_server.py` — the seven tools, mounted at `/mcp` | new `mcp_server.py`, `app.py`, `requirements.txt` |
| 7 | Force-mode ledger and backtest coverage | `tests/`, `tools/backtest.py` |
| 8 | HA entities for the new mode (`select` or two scripts) | servy `configuration.yaml` |
| 9 | Garage cover via the unattended-close path (HA spec Task 9) | `solar_routes.py`, `configuration.yaml` |
| 10 | Google Smart Home Action | Google Cloud console, `configuration.yaml` |

# 8. WHAT THE OWNER MUST DO

**Before `/mcp` is reachable (blocking):**

1. Generate three tokens (`openssl rand -hex 32`) and add `API_TOKEN`,
   `API_TOKEN_HA`, `API_TOKEN_MCP` to `.env`.
2. Add `TESLA_VIN` to `.env`.
3. Run `./deploy-macos.sh`; confirm the app agent restarted.
4. From a laptop: `curl http://192.168.87.84:8000/api/car/home` → **401**. If it
   returns 200, stop — the middleware is not live.

**To connect Claude:**

5. Claude Code: `claude mcp add --transport http tesla http://192.168.87.84:8000/mcp --header "X-Api-Key: <API_TOKEN_MCP>"`.
6. Claude Desktop and the phone app expect OAuth for remote connectors; a
   header-authenticated server needs the `mcp-remote` bridge. **Verify this
   before promising phone access** — it is the one connection detail not
   confirmed against a live client while writing this.

**For the garage (tasks 9 and 10):**

7. System Settings → Privacy & Security → **Local Network** → enable the Python
   interpreter. Two gotchas carried from the HA spec: the row often does not
   appear until the binary has *attempted* a LAN connection, so trigger a
   garage read first; and the grant lands on the **binary**, so it covers every
   script that interpreter runs.
8. Re-run `curl -H 'X-Api-Key: …' http://192.168.87.84:8000/api/car/garage` and
   confirm `reachable: true` before wiring the cover.

**For Google (task 10):**

9. Create a Google Cloud project and a smart-home Action; set fulfilment to
   `https://ha.darrenodom.com/api/google_assistant`; configure account linking;
   download a service account key for Report State.
10. Set `secure_devices_pin` in the `google_assistant:` block — Google requires
    it to *open* a garage cover.

**Do not do, ever** — carried forward unchanged from the HA spec §7: do not
install `tesla_fleet`; do not spend HA's single MQTT config entry; do not point
any HA or MCP client at `/api/dashboard`, `/api/car/state`, `/api/car/health`
or `/api/car/wake` for polling.
