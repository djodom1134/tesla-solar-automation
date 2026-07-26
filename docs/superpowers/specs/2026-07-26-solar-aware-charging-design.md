# Solar-aware charging — design

Adjust the car's charge rate continuously to match available excess solar, so
that charging consumes electrons that would otherwise be exported to the grid
rather than electrons imported from it.

This spec covers **sub-projects 1 and 2** of a five-part decomposition.

```
  ┌─ collector + HOME/geofence ──┐   THIS SPEC: foundation + controller
  │  (Task 5 + home.py + solar)  │
  └──────────────┬───────────────┘
                 ├──────────────┬───────────────┬──────────────┐
        solar charge         green            garage on        isochrone
        controller           accounting       arrival          range map
        THIS SPEC            spec 3           spec 4           spec 5
```

Build order: **foundation → controller → green graphs → garage → map.**

> **Revision note.** This document was adversarially reviewed before
> implementation; 78 findings were raised and 45 upheld, including four
> critical. The `headroom_w` symbol overload (§3), the cross-process token
> race (§2.4), and the inert `samples` migration (§4.1) were all corrected
> here. Where the original draft asserted something the repository
> contradicts, the correction is marked **[R]** so the reasoning is not
> silently lost.

---

## 1. Measured facts this design rests on

Measured against this account on 2026-07-26. Re-verify before trusting any of
it in a year. Rows marked *(inferred)* are **not** measurements — they are
external facts or derivations, flagged so a future reader can tell them apart.

### 1.1 The site

| Fact | Value | Source |
|---|---|---|
| `resource_type` | `solar` — **no Powerwall** | `/api/1/products` |
| `components.load_meter` | `true` | `site_info` |
| `components.battery` | `false` | `site_info` |
| Inverter | 1 × `1538000-89-E` (Tesla) | `site_info` |
| Installed | 2021-08-18 | `site_info.installation_date` |

`load_meter: true` is load-bearing. Without a consumption meter there is no
surplus signal and this project is impossible.

### 1.2 The surplus signal

`live_status` serves real-time data on a solar-only site. Measured over 39 polls
at 10-second spacing:

- **`solar_power` refreshes every ~10–30 s** with genuine movement.
- **`grid_power` refreshes every 60 s.** Run-lengths of identical values
  clustered hard at 6 × 10 s (median 60 s, mean 43 s).
- **`load_power` is derived, not independently metered** — `load = solar + grid`
  held exact to the decimal in every sample.
- The payload `timestamp` equals the poll time. The endpoint computes on demand.

**Consequence: the surplus signal is `grid_power`, refreshed at 60 s.** That
bounds the control loop period; a faster loop reads the same number twice.

**[R] The payload timestamp is therefore useless as a staleness detector** — it
is fresh by construction even when `grid_power` underneath it is a minute old.
See §5.

Sign convention (`energy.py:138-140`, re-verified): `grid_power > 0` is import,
`< 0` is export.

### 1.3 The prize

26 days of daily aggregates:

| | kWh/day |
|---|---|
| Solar production | ~46, consistent through July |
| **Exported to grid** | **median 13.8, mean 15.3, range 1.3 – 34.1** |
| House load | 28 – 148, bimodal |

```
2026-07-15   solar 47.3   home  28.4   export 33.6   import  14.7   ← car away
2026-07-21   solar 46.8   home  28.7   export 33.2   import  15.0   ← car away
2026-07-25   solar 41.7   home 148.0   export  1.4   import 107.7   ← car charging
```

*(inferred)* The bimodality is attributed to car charging. Corroborated by
`charge_energy_added: 58.38` in the fixture, but not directly measured — there
is no site-side car channel (§1.5) to prove it.

The goal is therefore not merely "capture the excess" but **move the car's
~60 kWh from imported electrons to solar ones.**

### 1.4 The control authority

| Fact | Value | Source |
|---|---|---|
| `charge_current_request_max` | 48 A | fixture (measured) |
| `charge_limit_soc` | 80, min 50, max 100 | fixture (measured) |
| Practical minimum | 5 A | *(inferred)* — the car's own UI floor. **Not** an API limit: `field-reference:311` records that `set_charging_amps` has no documented range and neither Tesla nor the proxy validates it. Sub-5 A additionally requires sending the command **twice** (the first call lands on 5). |
| Step size | 1 A ≈ 240 W | *(inferred)* from `field-reference:454` |
| Pack | ~100 kWh, so ~1 kWh per 1% | *(inferred)* — model spec, not measured |

### 1.5 What is NOT available — six probes, all empty

| Probe | Result | Consequence |
|---|---|---|
| `live_status.wall_connectors[]` | `[]` | No site-side car-power channel |
| `components.car_charging_data_supported` | key absent | — |
| `vehicle_charging_solar_offset_view_enabled` | `false` | Tesla's own solar-offset view unavailable |
| `telemetry_history?kind=charge` | endpoint OK, **0 rows** | No wall-connector history |
| `/api/1/dx/charging/history` | `null` | No session history |
| `grid_status` / `island_status` | `"Unknown"` / `"island_status_unknown"` | **No grid-outage guard is possible** — gateway fields, and there is no gateway |

No Tesla Wall Connector is commissioned to the energy site. **The control law
must not require a site-side measurement of the car's draw.** It may use the
car's *own* reported current (§3), which is a different thing.

### 1.6 History availability

| Call | Rows | Bucket | Backfill |
|---|---|---|---|
| `kind=energy&period=day`, past day | 288 | 5 min, metered, full source→destination split | **≥200 days**, one call/day |
| `kind=energy&period=day`, today | 96 at 07:55 | 5 min — **a partial day**, 00:00→now | — |
| `kind=power` | 288 | 5 min, instantaneous | **today only** |
| `kind=energy&period=month` | 1216 over 25 d | ~30 min | — |

**[R] The 96-row and 288-row responses are the same 5-minute bucket size.** The
96 was today truncated at the current time (96 × 5 min = 8 h = 00:00→07:55); a
completed day returns 288. The original draft presented these as contradictory
bucket sizes.

The `interval=15m` parameter is **ignored** — passing it and omitting it return
identical responses. Do not build on it.

### 1.7 Cost and limits

From Tesla's billing-and-limits page:

- **All responses with status < 500 are billable.** There is no free endpoint.
  **[R]** `config.py:75` says "Asleep uses the FREE state check only" — that is
  an unverified claim and must be corrected in the comment. `/vehicles/{vin}` is
  *cheap and sleep-safe*, which is what matters, but it is not free.
- Rate limits per device per account, **shared with every other app authorised
  on the account**: Realtime Data 60/min, Device Commands 30/min, Wakes 3/min.
- $10/month credit for individual developers.
- Commands ≈ $0.001 (Tesla's worked example: 1,211 commands → $1.21).
- **Per-request data pricing is unpublished.** The widely-cited $0.002 traces to
  a third-party blog from 2024.

**Call budget, corrected.** [R] The original "~215 data calls/day" was not
derivable from its own inputs. With the merged process of §2.1 the real figure
is:

```
6 h surplus window ÷ 120 s          = 180 ticks
  live_status                       = 180 calls   (energy)
  vehicle_data                      = 180 calls   (vehicle — same poll serves
                                                   history AND control, §2.1)
  set_charging_amps, write-on-change ≈  40 commands (empirical guess, logged)
                                      ────────────
                                      ~360 data + ~40 commands per active day
```

At the unverified $0.002 that is ~$22/month if every day is a charging day.
**This is why `period_s` is a config knob and why §3.5 caps spend in requests,
not dollars.**

### 1.8 Impossible — do not design around finding a way

**Garage door state is unreadable.** No field in the Fleet API, the CarServer
protobuf, or Fleet Telemetry. `trigger_homelink` is a bare toggle —
`VehicleControlTriggerHomelinkAction` carries only a `LatLong` and a `token`,
with no open/close parameter. If the door is open, "open" closes it. Many
openers cycle open→stop→close, so a press mid-travel stops the door partway.
`result: true` means the car keyed its transmitter, not that anything moved.

*(Nuance: myQ displays door state on the Tesla touchscreen under a paid
subscription, so the barrier is API surface, not physics. There is no developer
API, so the conclusion stands.)*

**The car's saved Home address is unreadable over REST.** It exists in the
protobuf (`ChargeState.home_location = 176`) reachable only via an undocumented
`vehicle_data_combo` endpoint value plus protobuf decoding. **Home is set by the
user.** Writing Home/Work or nav favourites is impossible.

---

## 2. Architecture

### 2.1 One background process, not two

**[R] The original draft never said where the control loop runs.** It also
implied a third process alongside the web app and the collector, which
multiplies the token hazard of §2.4 and duplicates every `vehicle_data` poll.

**The control loop lives inside `collector.py`,** which becomes the single
launchd-managed background agent (`com.tenxcious.tesla-collector`). It already
polls the car adaptively; the solar loop needs the same reads.

| Consequence | Why it matters |
|---|---|
| One `vehicle_data` poll serves history **and** control | Halves the call budget of §1.7 |
| `current_amps`, `charging_state`, `soc`, `lat/lon` all arrive together | Resolves the "where do the tick's vehicle facts come from" gap |
| Per-VIN serialisation is trivially satisfied | The signing proxy's per-VIN mutex 503s on concurrent traffic; one process cannot race itself |
| Only two processes hold tokens | §2.4 |
| The web app never commands the car automatically | Buttons remain the only web-app write path |

**Poll cadence becomes a function of mode**, replacing `next_interval()`'s
existing four-way rule with a five-way one:

```python
def next_interval(car_state, view, cfg, solar_engaged) -> int:
    if car_state != "online":     return cfg.poll_asleep
    if solar_engaged:             return cfg.solar_period_s   # 120 default
    if view is None:              return cfg.poll_idle
    if view.get("shift") in DRIVING: return cfg.poll_driving
    if view.get("charging"):      return cfg.poll_charging
    return cfg.poll_idle
```

`solar_engaged` is true when `solar_config.enabled` **and** the state machine is
in `charging`, `grace`, or `stopped`. The web app toggles `enabled` in the
database; the collector re-reads `solar_config` each tick, so no IPC is needed.

**When the loop is not engaged the collector behaves exactly as Task 5
specifies.** This spec does not modify the existing history behaviour.

### 2.2 New modules

| Module | Responsibility | Depends on |
|---|---|---|
| `home.py` | Home pin + radius storage; `classify(view, cfg) -> str` | pure + one table |
| `solar.py` | `control(tick) -> Decision`, `advance(state, tick, cfg) -> State` — both pure — plus the config/state accessors | `home` |
| `solar_routes.py` | `/api/car/solar/*` | `solar`, `home` |
| `static/setup.{html,js}` | Home pin, radius, mode config | the routes |

`control()` and `advance()` take plain values and return plain values — no
network, no car, no clock. `collector.py` owns all I/O and calls them.

### 2.3 Home is three-valued, never boolean

```python
def classify(view: dict, cfg: HomeConfig) -> str:   # "home" | "away" | "unknown"
```

- `unknown` — no coordinates. Tesla **omits** location keys rather than nulling
  them, so "scope revoked", "location sharing off", and "not home" are three
  different facts. Collapsing them is how a Supercharger session pollutes the
  green number later.
- `away` — outside the radius, **or** any DC fast charger.
- `home` — inside the radius **and** not a fast charger.

DC detection uses `fast_charger_present` **and** `fast_charger_type ∈
{Supercharger, Combo, Chademo, Gb}`. **[R] `derive()` surfaces neither
`fast_charger_present` nor a distinguishable type today** — it emits
`fast_charger` from `fast_charger_type` at `vehicle.py:78`. §4.2 lists the fix.

**[R] `conn_charge_cable` is not used.** The original draft claimed `SAE`/`IEC`
means AC; the repo's own field reference (`:116`) gives only the value set
(`IEC, SAE, GB_AC, GB_DC, SNA`) and says nothing about AC vs DC — and the
explicit `GB_AC`/`GB_DC` split is mild evidence against it. The conclusion
(cable type does not prove home) survives; the reasoning was unsourced.

`unknown` is surfaced in the UI, never defaulted — the same doctrine as
`GAP_SECONDS` in `store.history()`, which draws holes as holes.

**The controller acts only on `home`.** `unknown` freezes rather than restores
(§5), because restoring is itself a command and "we don't know where the car is"
is not grounds to send one.

### 2.4 Cross-process token refresh — CRITICAL

`tesla.py:88-92` documents it plainly: *"Tesla's refresh tokens are single-use
and rotate on every exchange. If we lose the new one, the user has to
re-authorize from scratch."* `TokenStore` caches in memory and
`TeslaClient._refresh_lock` is an `asyncio.Lock` — **meaningless across
processes.**

With the web app and the collector both running, two near-simultaneous refreshes
race: one wins, the other presents an already-consumed refresh token, gets a 401,
and may write a stale token over the good one. The cost is a full interactive
re-authorisation — the thing that just took a browser login to fix.

**This hazard is introduced by Task 5, not by this spec.** It must be fixed
before a second process ships.

Required change to `TokenStore`:

1. Take an **exclusive `flock` on the token file** before any refresh.
2. **Re-read from disk after acquiring the lock.** If another process already
   refreshed (the on-disk `access_token` differs or is unexpired), adopt it and
   do not refresh.
3. Refresh, write atomically (temp + rename — already implemented), release.
4. On 401 during refresh, re-read from disk once before surfacing
   `TeslaAuthError`; the other process may have just won the race.

Tests must cover two processes refreshing concurrently against a temp token
file, asserting exactly one network refresh and both processes ending with the
same valid token.

---

## 3. The control law

### 3.1 Symbols — each has exactly one meaning

**[R] The original draft used `headroom_w` for three different quantities**, and
the state machine compared an error signal against an absolute floor. A
converged loop holds the error near zero, so the machine dropped into `grace` on
every tick of successful charging — a permanent stop/start/wake cycle. Symbols
are now disjoint:

| Symbol | Definition | Used for |
|---|---|---|
| `grid_w` | signed, from `live_status`; >0 import | everything |
| `car_w` | `charger_actual_current × volts` when `charging_state ∈ {Charging, Starting}`, else **0** | display, `surplus_w` |
| `surplus_w` | `car_w − grid_w` — absolute solar available to the car | UI, logging, start test, §7.3 |
| `error_w` | `−grid_w − margin_w` — signed control error, driven to 0 | the control law only |
| SoC headroom | `(limit − soc) × pack_kwh / 100`, in **kWh** | §3.4 only |

`car_w` is pinned to *actual* charging, never to the standing `charge_amps`
setting: in `idle` and `stopped` the car draws nothing, and adding a nonexistent
1.2–11.5 kW would inflate `surplus_w` and start charging into surplus that does
not exist. This is what makes `charger_actual_current` load-bearing.

### 3.2 The law

```python
error_w    = -grid_w - margin_w
raw_target = current_a + error_w / volts          # UNCLAMPED float — floor test only
step_a     = int(round(clamp(error_w / volts, -ramp_a, +ramp_a)))
target     = clamp(current_a + step_a, min_a, max_a)   # always an int

if abs(error_w) < deadband_w:
    return NO_CHANGE
if target != last_acknowledged_a:
    set_charging_amps(target)
```

`raw_target` stays a float — it exists only to answer "would the law have asked
for less than `min_a`?". `target` is rounded to an integer before it is ever
sent, because `charging_amps` is an integer field and a fractional command is
not a thing the car accepts.

**Incremental, not absolute.** `grid_power` already contains the car's draw. An
absolute form (`solar − load`) creates positive feedback: raise amps →
`load_power` rises → apparent surplus collapses → controller backs off →
oscillation. The incremental form converges on `grid ≈ −margin_w` and rejects
house disturbances as a matter of course.

**The floor test is `raw_target < min_a`** — not a watts comparison. This is
correct in every state because it is expressed in the same units the controller
commands, and it derives the floor from `volts` and `margin_w` rather than
hardcoding 1200 W (which would be wrong if §8.4's voltage check finds the site
is not 240 V).

Worked, at 240 V / margin 100 / min 5 A:

| Situation | `grid_w` | `error_w` | `raw_target` | breach? |
|---|---|---|---|---|
| converged, 3 kW sun, car 12 A | −120 | +20 | 12.1 | no |
| converged, 9.6 kW sun, car 40 A | 0 | −100 | 39.6 | no |
| cloud: 800 W sun, car held 5 A | +400 | −500 | 2.9 | **yes** |

**Voltage.** `charger_voltage` reads `2`, not `0`, when idle
(`field-reference:97`). Trust it only when `charging_state ∈ {Charging,
Starting}`; cache the last valid value; default 240 V. Never use `charger_power`
— integer kW, so 7.7 kW reads 7 or 8.

**Stability.** The meter updates at 60 s and the loop ticks at 120 s. One amp
≈ 240 W, so the 250 W deadband is the smallest that cannot oscillate. `ramp_a`
= 8 prevents slamming 5 A → 48 A when a cloud clears.

### 3.3 State machine

```
 disabled ──enable──▶ idle

 idle ──home ∧ plugged ∧ surplus_w ≥ start_w for 60 s──▶ charging
                                    start_w = min_a × volts + margin_w  (1300 W)

 charging ── run §3.2 each tick
      └── raw_target < min_a on 2 CONSECUTIVE ticks ──▶ grace
 grace   ── hold min_a, timer running
      └── raw_target ≥ min_a + 1 on 2 consecutive ticks ──▶ charging (cancel timer)
      └── timer > grace_s ──▶ stopped
                              (charge_stop; restore amps AND limit)
 stopped ── surplus_w ≥ start_w sustained restart_hold_s ──▶ charging
                              (wake if needed; charge_start; then §3.2)

 any ── ¬plugged ∨ away ∨ disabled ──▶ idle  (restore amps AND limit)
 any ── unknown ──▶ freeze in place, take no action  (§5)
```

**[R] Four corrections from review are folded in:**

- **Two-tick dwell on `charging → grace`.** `grid_w` refreshes at 60 s while
  `charger_actual_current` comes from the car's own clock. For up to one meter
  period after an amps write, the car's draw has moved and the meter has not, so
  `raw_target` is transiently wrong by Δamps. A single-tick trigger would flap.
- **Hysteresis on `grace → charging`** (`min_a + 1`, two ticks), or a surplus
  hovering at exactly the floor chatters every tick.
- **`grace → stopped` restores the charge limit, not just amps.** `stopped` is
  where the loop lands at every sunset, and its only exit needs sunlight — so
  the original draft left the car sitting overnight at a raised limit, which is
  precisely the NCA dwell case §3.4 exists to avoid.
- **`stopped → charging` issues `charge_start`.** Setting amps on a stopped car
  changes a setting and draws nothing. The original state machine had no
  `charge_start` anywhere.

**Ramp exception, stated explicitly:** entering `grace` writes `min_a`
immediately, bypassing `ramp_a`. A car at 30 A that loses sun must not descend
over four ticks while importing. Down-to-floor is the one permitted violation of
the ramp limit, and it is safe because it can only reduce draw.

**`plugged` is defined as `charging_state ∉ {"Disconnected", None}`.** [R] Not
`view["plugged_in"]`, which is `_s(conn_charge_cable) is not None`
(`vehicle.py:74`) and disagrees with §5's unplugged detector. One predicate,
used in both places.

**Grace exists because the floor is hardware.** Below `min_a` the car cannot
charge at all, and a Colorado afternoon drops surplus below ~1.3 kW repeatedly.
Hard-stopping each time would cycle the contactor, reset `charge_energy_added`,
spend a rate-limited wake per restart, and — per evcc — some vehicles refuse to
restart after frequent interruption without a physical replug.

**Grace import is metered.** `import_w = max(grid_w, 0)`, logged every tick. The
counter shown in §7.2 sums `import_w × period_s` **only over ticks where
`state == 'grace'`** — [R] otherwise it would total ordinary night-time house
import and the no-grid-electrons claim would become meaningless.

**[R] Honest cost of grace.** Worst case is 1.2 kW × `grace_s`; at 180 s that is
60 Wh *per event*. The original draft called this "0.4% of a median day" while
also saying such events fire 20+ times daily — 20 × 60 Wh = 1.2 kWh ≈ **8.7%**
of a median day's export. The real figure will be far lower because grace ends
early whenever sun returns, but the honest bound is the one to display, and the
metered counter is the one to believe.

**Restarting from `stopped` requires waking the car**, so it demands
`restart_hold_s` (default 300) of sustained surplus. Everywhere else the loop
never wakes the car.

### 3.4 Charge-limit raise

SoC headroom, not surplus, is the binding constraint. At an 80% limit and 76%
SoC there were **4 kWh** of headroom against a **median 13.8 kWh/day** of export.

| Ceiling | SoC headroom | vs. median day (13.8 kWh) |
|---|---|---|
| 80% (today) | 4 kWh | 29% |
| 90% (default) | ~14 kWh | 101% |
| 100% (max) | ~24 kWh | 174% |

**[R]** The original draft's "median plus most of a max day" for the 100% row
was wrong — 24 kWh is 70% of a max day (34.1), not more than it.

Triggers, made evaluable [R] — the original stated intent without thresholds:

- **Raise** when: state is `charging`, `soc ≥ limit − 2`, and `surplus_w ≥
  start_w` has held for `raise_hold_s` (default 600). One `set_charge_limit`
  per engagement — guarded by `solar_state.raised_to`, never re-issued while
  already raised.
- **Revert** on any transition to `idle` or `stopped`, and on startup recovery.
- `raise_limit = 0` disables the feature entirely; §7.1 exposes it as the
  ceiling slider's "off" position.

On an NCA pack, degradation comes from **dwelling** at high SoC, not reaching
it. The revert policy matters more than the ceiling. The raised limit is
displayed **next to the original** so a stuck raise is visible.

### 3.5 Spend cap

**[R] The original draft named a spend cap in two sections and defined it
nowhere.** It is denominated in **requests, not dollars**, because §1.7
establishes that per-request data pricing is unpublished.

- `solar_config.daily_request_cap INTEGER NOT NULL DEFAULT 1200`
- Counter lives in `solar_state.requests_today` with `requests_day` (a local
  date string from `settings.timezone`).
- Reset when `requests_day != today`; the boundary is **local midnight**,
  matching every other date boundary in this project.
- On reaching the cap the loop moves to `idle` (restoring amps and limit) and
  sets a `capped` flag surfaced prominently in §7.2. It does not resume until
  the next local day.

Hitting *Tesla's* limit instead disables the entire application and permanently
deletes any Fleet Telemetry configuration — hence a self-imposed cap well below
it.

### 3.6 Crash safety

`solar_state` records `original_amps` and `original_limit` with a `dirty` flag,
written **before** any modification. The hazard is concrete: the car remembers
its amp setting per GPS location, so a crash mid-session leaves the next
unattended overnight charge starting at 5 A.

**[R] Startup restore is best-effort, not a synchronous precondition.** The
original draft required restoration "before the loop starts", which is
impossible when the proxy is down or the car is asleep — and invariant §3.7.1
forbids waking it. Corrected semantics:

```
on startup, if dirty:
    if classify() != "home":   leave dirty set, do not restore, warn in UI
    if car not online:         leave dirty set, retry next tick
    if proxy down:             leave dirty set, retry next tick
    else:                      restore amps + limit, verify by readback, clear dirty
the loop refuses to enter `charging` while dirty is set
```

The `classify() != "home"` gate exists because [R] an unconditional restore
would write `original_amps` into a **Supercharger session** — the exact hazard
§2.3 claims is structurally impossible.

### 3.7 Invariants

1. Never wake the car for the loop, except the `stopped → charging` restart.
2. **Read back `charge_current_request` after every write.** `result: true` is
   compatible with silent clamping — requests above `charge_current_request_max`
   are **clamped** (the value changes to the ceiling). [R] This differs from
   `set_charge_limit`, which is a silent **no-op** (`commands.py:65-68`) — the
   value does not change at all. The original draft called them the same thing.
   The controller integrates against `last_acknowledged_a`, never against what
   it asked for.
3. One in-flight request per VIN — satisfied structurally by §2.1.
4. Honour `429` / `Retry-After` by lengthening the period, never retrying.
5. Respect the §3.5 request cap.
6. Act only on `home`.

---

## 4. Data model

```sql
CREATE TABLE IF NOT EXISTS home_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  latitude REAL NOT NULL, longitude REAL NOT NULL,
  radius_m INTEGER NOT NULL DEFAULT 100,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS solar_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  enabled INTEGER NOT NULL DEFAULT 0,
  period_s INTEGER NOT NULL DEFAULT 120,
  margin_w INTEGER NOT NULL DEFAULT 100,
  deadband_w INTEGER NOT NULL DEFAULT 250,
  ramp_a INTEGER NOT NULL DEFAULT 8,
  min_a INTEGER NOT NULL DEFAULT 5,
  grace_s INTEGER NOT NULL DEFAULT 180,
  restart_hold_s INTEGER NOT NULL DEFAULT 300,
  raise_hold_s INTEGER NOT NULL DEFAULT 600,
  soc_ceiling INTEGER NOT NULL DEFAULT 90,
  raise_limit INTEGER NOT NULL DEFAULT 1,
  daily_request_cap INTEGER NOT NULL DEFAULT 1200,
  deadline_soc INTEGER, deadline_hour INTEGER,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS solar_state (
  vin TEXT PRIMARY KEY,
  state TEXT NOT NULL DEFAULT 'idle',
  dirty INTEGER NOT NULL DEFAULT 0,
  original_amps INTEGER, original_limit INTEGER, raised_to INTEGER,
  requests_today INTEGER NOT NULL DEFAULT 0,
  requests_day TEXT,
  capped INTEGER NOT NULL DEFAULT 0,
  engaged_at INTEGER, updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS solar_ticks (
  ts INTEGER NOT NULL, vin TEXT NOT NULL,
  state TEXT NOT NULL,
  grid_w REAL, solar_w REAL, car_w REAL, surplus_w REAL, error_w REAL,
  amps_before INTEGER, amps_target INTEGER, amps_written INTEGER,
  soc INTEGER, import_w REAL, note TEXT,
  PRIMARY KEY (vin, ts)
);
```

`solar_ticks` logs **every** tick, written or not, so a quiet loop and a broken
loop look different. [R] It carries both `error_w` and `surplus_w` now that they
are distinct quantities.

`engaged_at` is set on entry to `charging` and is what §7.2 renders as session
duration. `raised_to` guards §3.4's one-raise-per-engagement rule.

### 4.1 Migration — and the writer, or it is inert

**[R] This is the correction the review called critical.** `store.record()`
(`store.py:69-80`) writes a hardcoded 15-column `INSERT`. Adding columns via
`ALTER TABLE` alone produces five permanently-NULL columns and captures nothing
— so the section's own justification ("`charge_energy_added` cannot be
backfilled — every day of delay is permanently missing") would not have been
satisfied by the change originally specified.

Three coordinated edits are required:

```python
# 1. store.py — idempotent migration (CREATE TABLE IF NOT EXISTS will not do it)
def _migrate(db):
    cols = {r["name"] for r in db.execute("PRAGMA table_info(samples)")}
    for name, decl in (("charge_energy_added", "REAL"),
                       ("charger_actual_current", "INTEGER"),
                       ("charger_voltage", "INTEGER"),
                       ("fast_charger_present", "INTEGER"),
                       ("fast_charger_type", "TEXT"),
                       ("at_home", "TEXT")):
        if name not in cols:
            db.execute(f"ALTER TABLE samples ADD COLUMN {name} {decl}")

# 2. store.record() — extend the INSERT column list and the value tuple to match.
# 3. vehicle.derive() — emit the keys record() will read (see §4.2).
```

**Rows written before the migration stay `NULL` forever.** Every consumer must
treat `NULL` as *unknown*, never as zero.

### 4.2 Upstream fixes — corrected against the source

**[R] The original draft's list was wrong in both directions.** `derive()`
already surfaces two of the three fields it claimed were missing, so an engineer
following it would have written duplicate keys.

| Field | Status today | Action |
|---|---|---|
| `charger_actual_current` | **already emitted** as `amps_actual` (`vehicle.py:68`) | none — use the existing key |
| `fast_charger_type` | **already emitted** as `fast_charger` (`vehicle.py:78`) | none |
| `plugged_in` | **already emitted** (`vehicle.py:74`) | not used by this spec (§3.3) |
| `charger_voltage` | absent | **add** — §3.2 needs it |
| `fast_charger_present` | absent | **add** — §2.3's primary away-signal |
| `homelink_nearby` | absent | **add** — corroborating signal only |
| `homelink_device_count` | absent | **add** (measured: 2 on this car) |

Plus one independent fix: **`energy.derive()` computes
`grid_energy_exported_from_solar` at `energy.py:63` and discards it.** It is the
headline input for spec 3's "excess" graph, and surfacing it makes
`summarize()`'s `self_consumption` exact rather than the `min(grid_export,
solar)` approximation at `energy.py:115`, which is wrong whenever anything but
solar exports.

---

## 5. Failure handling

The rule is **freeze, do not guess.**

| Failure | Detected by | Response |
|---|---|---|
| Car asleep | `state != online` | idle; never wake |
| `vehicle_data` 408 | `VehicleAsleep` | treat as asleep |
| **`grid_power` stuck** | same value > 5 consecutive ticks | hold amps, flag suspect |
| `grid_power` absent | `.get()` → None | skip the tick |
| `live_status` request fails | exception | hold amps, retry next tick |
| 429 | status | lengthen period, honour `Retry-After` |
| Proxy down | connect error | freeze; leave `dirty` set; surface |
| Proxy 503 | status | mutex contention; retry next tick only |
| Write clamped | readback ≠ target | adopt actual as `last_acknowledged_a`, log |
| Unplugged | `charging_state == "Disconnected"` | restore, → idle |
| Drove away | `classify() == "away"` | restore, → idle |
| **Location unknown** | `classify() == "unknown"` | **freeze, take no action** |
| Crash | `dirty` at startup | best-effort restore per §3.6 |
| Request cap | §3.5 counter | restore, → idle, set `capped` |

**[R] The staleness guard was removed.** The original draft checked
`live_status` payload timestamp age > 5 min — but §1.2 measured that the
timestamp *equals the poll time*, so it is fresh by construction and the guard
could never fire. That is the exact anti-pattern this section condemns. The real
staleness risk is `grid_power` itself sitting at 60 s resolution, which the
stuck-value check above actually detects.

**[R] `unknown` freezes; it does not restore.** The original draft's `classify()
!= home` row collapsed `unknown` into `away`, defeating the three-valued type
built in §2.3. Restoring is itself a command, and "we don't know where the car
is" is not grounds to send one.

**No grid-outage guard is possible** (§1.5). Do not write one.

---

## 6. Testing

1. **Pure-function tables.** `control()` and `advance()` take plain values.
   Cover every row of the §3.2 worked table, both dwell paths, both hysteresis
   bands, and the ramp exception.

2. **Backtest against real weather.** [R] The adapter must be specified, because
   the two quantities are not the same kind. `calendar_history?kind=energy`
   returns **Wh per 5-minute bucket, split by source and destination**; the
   controller consumes **instantaneous signed watts**. Conversion:

   ```
   grid_w(bucket) = (grid_energy_imported
                     - grid_energy_exported_from_solar
                     - grid_energy_exported_from_battery
                     - grid_energy_exported_from_generator) * 12
   ```

   (×12 converts Wh per 5 min to average W.) This is an *average over the
   bucket*, so the backtest under-represents sub-5-minute cloud transients and
   will therefore **understate** stop/start churn. State that in the test.

   Assert: total grace import within budget, capture within *x*% of theoretical
   maximum, stop/start cycles bounded.

   **Clean days only** — the buckets contain the car's own historical draw, so
   replaying a day when the car charged at home double-counts it. Use
   `2026-07-14`, `07-15`, `07-21` (house ~28 kWh, export ~33 kWh).

3. **Cross-process token refresh** (§2.4) — two processes, one temp token file,
   assert exactly one network refresh and a consistent final token.

4. **Fault injection** for every row of §5, especially the clamped-write path.

5. **Crash recovery** — `dirty` set, restart, assert the three §3.6 gates.

6. **Demo mode.** [R] The car-page spec (§9) argues demo mode is not a nicety
   but the only way to build UI without burning calls or waking the car, and
   `demo.py` is 351 lines threaded through `app.py` and `car_routes.py`. This
   spec adds a whole page and a status card; both need `DEMO=1` fixtures —
   a synthetic surplus curve and each of the five states.

7. **Browser verification** against `DEMO=1`: every state renders, light and
   dark, and the map pin/radius round-trips through the API.

---

## 7. UI

### 7.1 `/setup.html`

1. **Home** — Leaflet map, draggable pin seeded from the car's current position
   when available, radius slider 25–500 m (default 100), circle drawn live.
   Displays the *current* classification so the pin can be verified before it is
   trusted.
2. **Solar charging** — master toggle; period 120 s / 60 s; ceiling slider
   80–100 with an explicit "off" position writing `raise_limit = 0`; grace
   minutes. `margin_w`, `deadband_w`, `ramp_a`, `min_a`, `daily_request_cap`
   behind a disclosure — tuning knobs, not decisions.
3. **Deadline warning** — target SoC and time.

### 7.2 Car page

A solar card showing state, `surplus_w`, present amps, session duration from
`engaged_at`, and — when raised — **the raised limit beside the original**.
Plus the grace-import counter in Wh (today and cumulative) and, when set, the
`capped` and `dirty` warnings.

### 7.3 Deadline warning: observed, not forecast

Forecasting tomorrow's sun needs a weather API and will sometimes be wrong. A
wrong forecast in a feature whose selling point is honesty is a bad trade.

Fire on **observed** end-of-day instead: when `solar_w < 200 W` **and**
`surplus_w < start_w`, both continuously for 30 minutes, compare actual SoC to
`deadline_soc`.

> *"Solar is done for today. You're at 62%. You asked for 70% by 7 AM."*

**[R] No astronomy.** The original draft said "30 minutes past solar noon, or at
civil sunset" — two different readings, and both need a sunrise/sunset
dependency the section claims not to have. Observed darkness needs none.

**It never acts.**

### 7.4 Honesty rules

- `unknown` location displays as unknown, never defaults to home.
- Grace import is metered from `grid_w`, summed only over `grace` ticks.
- The backtest's averaging limitation is stated where its results are shown.

---

## 8. Open items requiring live verification

1. **Actual Fleet API spend.** Per-request data pricing is unpublished (§1.7).
   Check the developer dashboard after one week and tune `period_s` and
   `daily_request_cap`. Confirm a payment method and a non-zero billing limit
   exist — a $0 limit auto-disables the application.
2. **`homelink_nearby` in the garage.** It read `false` in the fixture alongside
   `sentry_mode: true`, suggesting the car was not home. Read it while parked at
   home. It is a black box with an undocumented radius either way, so the pin
   remains primary.
3. **Site voltage.** 240 V is assumed. Confirm from `charger_voltage` during a
   real session; §3.2's floor test derives from it, so a different value is
   handled automatically once measured.
4. **`charge_current_request_max` stability.** [R] It is the *request ceiling*,
   which `field-reference:454` distinguishes from `charger_pilot_current`, the
   EVSE's advertised ceiling. Cache per session; invalidate on any transition
   through `Disconnected`.
5. **The 5 A floor** (§1.4) is the car's UI floor, not an API limit. If a lower
   floor proves stable, `min_a` is a config knob — but sub-5 A requires the
   double-send, which `commands.py:71` does not implement.

---

## 9. Explicitly out of scope

- **Garage automation — spec 4.** The decision is recorded (build it blind, with
  a one-shot latch armed on geofence exit, open-only, no retry ever, a 60 s
  freshness gate on the injected coordinates, shipped disabled behind an
  explicit confirmation that native HomeLink auto-open is off). [R] It is *not*
  built here: the original draft designed it in §8 while the header assigned it
  to spec 4, and leaked its config columns and UI into three other sections.
  Those are removed.
- **Green accounting and graphs — spec 3.** Blocked on this spec's migration
  landing and accruing data.
- **Isochrone range map — spec 5.** Research complete
  (`docs/superpowers/research/2026-07-26-isochrone-options-raw.json`);
  HERE Isoline v8 `consumption` mode is the recommendation.
- **Fleet Telemetry as the read side.** Would give push location and separate
  `ACChargingEnergyIn` / `DCChargingEnergyIn` monotonic counters, removing every
  `charge_energy_added` reset hazard — but needs a publicly reachable FQDN, a
  real TLS chain, and mTLS. `localhost:4443` cannot host it.
- **Powerwall coordination.** There is no Powerwall (§1.1).
