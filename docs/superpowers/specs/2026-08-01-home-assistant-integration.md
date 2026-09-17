# Home Assistant Integration — Design

**Date:** 2026-08-01
**Status:** plan, not yet implemented.

## Verified before writing this

Every claim below that decides the architecture was checked against the live
systems, not taken from the research:

| Claim | How verified | Result |
|---|---|---|
| HA's MQTT integration accepts only ONE broker | read `mqtt/manifest.json` in the running container | `single_config_entry: True` -- confirmed |
| The `rest:` platform cannot provide number/button/cover | listed `components/rest/*.py` | only sensor, binary_sensor, switch, notify -- confirmed |
| `template:` CAN provide them | listed `components/template/*.py` | number, button, cover, select, switch, device_tracker all present |
| `rest_command` exists | listed components | present |
| Energy Dashboard needs total/total_increasing | developers.home-assistant.io sensor entity docs | "requires state_class TOTAL or TOTAL_INCREASING"; `measurement` is NOT eligible |
| `total_increasing` reset handling | same doc | a drop is "the start of a new meter cycle"; 10% tolerance; zero-point becomes 0 |
| HA can reach our service | curl from inside the HA container | `HTTP 200` |
| Our service can reach HA / the ratgdo | socket test on the mini | HA:8123 OPEN, ratgdo:80 OPEN *from a shell*; still blocked from launchd |
| servy's mosquitto is LAN-unreachable | socket test from the mini | `ConnectionRefusedError` -- loopback-only, as expected |

## Three defects in OUR code, found while designing this

All three verified by reading the source; all three are cost or honesty bugs
that exist regardless of whether the HA integration is ever built.

1. **`daily_request_cap` protects nothing outside the collector.**
   `solar.count_request()` is called at `collector.py:284` and nowhere else.
   `/api/car/state`, `/api/car/wake` and `/api/dashboard` spend Tesla
   requests entirely outside the cap -- a wall tablet polling the dashboard
   would burn the budget while `requests_today` reported a comfortable
   number. The cap reports on a path it does not guard.

2. **`TESLA_VIN` is unset**, so `car_routes._vin()` calls
   `client.resolve_vin()`, which falls through to `GET /api/1/vehicles`.
   Every state/history/health/wake request pays an extra billed call.

3. **`deadline_soc` / `deadline_hour` are dead code.** Present in the DDL,
   in `STATE_DEFAULTS`, in `CONFIG_BOUNDS`, in `NULLABLE_CONFIG_FIELDS` and
   on the setup page -- and read by nothing in the control path. They look
   like a working "be charged by 7am" feature and are not one. Either
   implement or delete; exporting them to HA would manufacture an automation
   the owner would trust and that would never fire.

---

# HA ↔ tesla_automation: implementation plan

---

## 1. THE MECHANISM

**REST polling. HA pulls from us. No MQTT, no broker, ever.**

Three sentences that decide it: HA→`10.0.0.84:8000` is already proven working and trips neither hard constraint (inbound to the mini is not TCC-gated; nothing new runs on servy). MQTT costs HA's *only* config entry permanently (`"single_config_entry": true` in `mqtt/manifest.json`), a fourth launchd daemon, a `paho-mqtt` dependency, broker auth (declaring `listener 1883 0.0.0.0` flips `allow_anonymous` to false → HA gets rc=5), and an inbound macOS ALF prompt that ad-hoc-signed mosquitto will raise — all to push data that changes every 120–1800 s. The claimed MQTT advantage of "REST fails closed for free" is *false* (`rest/data.py` nulls only on `TimeoutError`/`ClientError`, never on HTTP status), so both designs cost one availability key per entity; MQTT wins nothing and loses the slot.

### What gets installed/configured, where, and which constraint it trips

| Step | Host | Constraint tripped |
|---|---|---|
| New `ha_routes.py` + `auth.py` in our FastAPI app; restart `com.example.tesla-app` | Mac mini | none (loopback + inbound only) |
| New `meters` + `collector_heartbeat` tables in `car.db` | Mac mini | none |
| Hourly `calendar_history` ingest inside the existing collector (task 8) | Mac mini | none — routed internet, not LAN |
| `rest:` / `rest_command:` / `template:` blocks in `/config/configuration.yaml`, one HA restart | servy | **cooling: OK.** 2 HTTP GETs/min, no compile, no new container, no new process |
| Energy Dashboard + area/rename config in HA UI | servy | none |
| Garage cover (task 9) | Mac mini → 10.0.0.78 | **macOS Local Network gate: BLOCKED.** Requires the one GUI grant. Deferred behind it |
| Nothing at all | servy's mosquitto snap | untouched — its HiveMQ/ALPR world is left alone and HA's MQTT slot stays free |

**Rejected explicitly:** MQTT discovery (above); `POST /api/states` (no control, no unique_id, no restart survival); `command_line` (a worse `rest:` that shells out inside the overheating container); installing `tesla_fleet` (a second uncoordinated commander on the same car and a second poller on the already-429ing energy site); reflashing the ratgdo (does not fix the TCC gate).

**Structural invariant that makes the cost rule enforceable:** `ha_routes.py` must never import `tesla.py` or the Fleet client. A unit test asserts this. HA is physically incapable of spending a Tesla request.

---

## 2. THE ENTITY TABLE

All entities are YAML `rest:`/`template:` entities. **Honest caveat: YAML REST/template entities get a `unique_id` (renameable, areable, HomeKit-exportable) but never a device-registry entry.** There is no device card. Grouping below is by name prefix + HA area, which is what the UI will actually show.

Two `rest:` resources only:
- **R1** `http://10.0.0.84:8000/api/ha/state` — `scan_interval: 60`
- **R2** `http://10.0.0.84:8000/api/ha/meters` — `scan_interval: 300`

### Group A — "Solar Charger" (R1)

| Component | Name | Source field | device_class | state_class | Unit | Category | Writable |
|---|---|---|---|---|---|---|---|
| sensor | Solar Charger State | `state` | enum | — | — | — | no |
| sensor | Solar Charger Surplus | `surplus_w` | power | measurement | W | — | no |
| sensor | Solar Charger Amps | `amps` | current | measurement | A | — | no |
| sensor | Solar Charger Solar Share | `charged_solar_share` | — | measurement | % | — | no |
| sensor | Solar Charger Banked Miles | `banked_miles` | distance | measurement | mi | — | no |
| sensor | Solar Charger Requests Today | `requests_today` | — | total_increasing | — | diagnostic | no |
| sensor | Solar Charger Grace Import Today | `grace_import_wh_today` | — | total_increasing | Wh | diagnostic | no |
| binary_sensor | Solar Charger Rate Limited | `rate_limited` | problem | — | — | diagnostic | no |
| binary_sensor | Solar Charger Cap Reached | `capped` | problem | — | — | diagnostic | no |
| binary_sensor | Solar Charger Restore Pending | `dirty` | problem | — | — | diagnostic | no |
| binary_sensor | Solar Charger Ledger Stale | `ledger_stale` | problem | — | — | diagnostic | no |
| binary_sensor | Solar Charger Collector Offline | `not collector_running` | problem | — | — | diagnostic | no |
| switch | Solar Charging | `enabled` | — | — | — | — | **yes** |
| switch | Solar Raise Charge Limit | `raise_limit` | — | — | — | — | **yes** |
| number | Solar Export Margin | `margin_w` | power | — | W | config | **yes** |
| number | Solar Minimum Amps | `min_a` | current | — | A | config | **yes** |
| number | Solar SoC Ceiling | `soc_ceiling` | — | — | % | config | **yes** |
| number | Solar Grace Budget | `grace_budget_wh` | — | — | Wh | config | **yes** |

`Grace Import Today` deliberately carries **no** `device_class: energy` — that keeps it out of the Energy Dashboard picker, where it would double-count against grid consumption (`energy.py:19-21`: the site's `grid_energy_imported` already contains it).

### Group B — "Tesla" (R1)

| Component | Name | Source field | device_class | state_class | Unit | Category | Writable |
|---|---|---|---|---|---|---|---|
| sensor | Tesla Battery | `view.soc` | battery | measurement | % | — | no |
| sensor | Tesla Charge Limit | `view.limit` | battery | measurement | % | — | no |
| sensor | Tesla Range | `view.range_mi` | distance | measurement | mi | — | no |
| sensor | Tesla Odometer | `view.odometer` | distance | total_increasing | mi | diagnostic | no |
| sensor | Tesla Data Age | `snapshot_age_s` | duration | measurement | s | diagnostic | no |
| binary_sensor | Tesla Plugged In | `view.plugged_in` | plug | — | — | — | no |
| binary_sensor | Tesla Charging | `view.charging_state == "Charging"` | battery_charging | — | — | — | no |
| binary_sensor | Tesla At Home | `classification == "home"` | presence | — | — | — | no |
| button | Wake Tesla | `POST /api/car/wake` | — | — | — | — | **yes** |

**No `device_tracker`, deliberately.** The `rest:` platform offers only `sensor`/`binary_sensor`, and more importantly `home.py` is three-valued: Tesla *omits* location keys rather than nulling them, so "scope revoked", "sharing off" and "not home" arrive identically. `unknown → not_home` would fire every "car left home" automation each time the car falls asleep in the garage — i.e. daily. A `presence` binary_sensor that goes **unavailable** on `unknown` is the faithful port. No lat/lon crosses the wire.

### Group C — "Energy" (R2) — **all six are Energy Dashboard sources**

| Component | Name | Source field | device_class | state_class | Unit | Category | Writable |
|---|---|---|---|---|---|---|---|
| sensor | Tesla Charge Energy Total | `meters.car_total_kwh` | energy | total_increasing | kWh | — | no |
| sensor | Tesla Charge Energy From Solar | `meters.car_solar_kwh` | energy | total_increasing | kWh | — | no |
| sensor | Tesla Charge Energy From Grid | `meters.car_grid_kwh` | energy | total_increasing | kWh | — | no |
| sensor | Site Grid Import | `meters.site_import_kwh` | energy | total_increasing | kWh | — | no |
| sensor | Site Grid Export | `meters.site_export_kwh` | energy | total_increasing | kWh | — | no |
| sensor | Site Solar Production | `meters.site_solar_kwh` | energy | total_increasing | kWh | — | no |

### Explicitly NOT exposed, and why

- **Live site power (`solar_w`, `grid_w`, home load).** `solar_ticks` only gains a row when a tick runs; the collector stands down after dark and writes nothing when solar is disabled or the car is away. A power sensor unavailable for most of the day is noise, and there is no `load_power`/`battery_power` column to derive home load honestly. Our own page at `:8000` already shows live power.
- **`grid_net`.** The only true `total` we own. On a 3:1 tariff a single net meter values the noon export kWh at the import rate and understates cost by $0.08 every round trip. Two one-way meters, always.
- **Home consumption.** HA computes it as `import + solar − export`. Feeding ours creates two disagreeing numbers on one screen.
- **Every other ratio and miles basis** (`banked_pct`, `free_miles_share`, `self_sufficiency`, `banked_miles_rated`/`_measured`, `tracked_miles`, `accrual_mi_per_s`). Wrong dimension or already-labelled approximations; `solar_routes.py` records that the rated and nominal-pack bases disagree ~30% on this car.
- **Battery slots.** No home battery. Configuring them creates permanently empty cards.
- **Per-door / per-window / per-seat / per-tyre entities.** Each is a reason to call `vehicle_data` more often and drives no automation here.
- **Force charge (button or switch), stop charging.** `advance()` in `idle` with `car_charging=True` now *adopts* the charge: a forced night start is adopted, floor-breaches within two ticks, transits grace and stops — ~4 billed commands to end where it began. If ever wanted it must be `select.charge_mode` (Solar only / Charge now / Off) implemented *inside* the collector with a midnight expiry, not a command the loop reverses.
- **Every command with `confirm=True`** in `commands.py` — not just `risk="high"`. `confirm` is the human-in-the-loop marker; `risk` is a display hint. A `risk == "high"` filter leaks `schedule_software_update` and `speed_limit_set_limit`. `door_unlock` as an HA entity is an unlock reachable by any automation.
- **`trigger_homelink`.** A stateless one-way toggle; a second way to move the same door is how HA ends up believing a door is closed when it isn't.
- **`garage_url` as a text entity.** An SSRF primitive reachable from any HA automation. Setup page only.
- **Home lat/lon/radius.** Changing `radius_m` retroactively redefines what counted as a home charge for the attribution split, the banked ledger and the garage arrival latch.
- **The `json_attributes` carrier-sensor pattern.** 14 attributes changing every poll = ~2,880 `state_attributes` rows/day written on the box with the 105 °C trip. One flat sensor per value instead.

---

## 3. THE CONTROL SURFACE

Every writable is `template:` switch/number wrapping a **dedicated** `rest_command` (one per field — never a generic `{{ key }}: {{ val }}` command, which is a skeleton key to all ~20 `CONFIG_BOUNDS` entries including `period_s` and `daily_request_cap`). Every `set_value` passes `{{ value | int }}` (or `| float` for `grace_budget_wh`): `solar_routes.py:344` does `int(value)` and `int("250.0")` raises → silent 400, slider snaps back, nothing logged. Every write is followed by `homeassistant.update_entity` on its source sensor (a **re-read**, not an echo of what we sent — the backend silently coerces). Switches carry `optimistic: true` so the toggle doesn't feel dead during the 10 s coordinator debounce; the re-read reconciles.

| Entity | Field | API bound | **HA bound** | Step | Justification for the tighter bound |
|---|---|---|---|---|---|
| switch Solar Charging | `enabled` | 0–1 | 0–1 | — | — |
| switch Solar Raise Charge Limit | `raise_limit` | 0–1 | 0–1 | — | Consent, not tuning: it decides whether an NCA pack parks high |
| number Solar Export Margin | `margin_w` | 0–2000 | **0–500** | 25 | The loop converges on `grid ≈ −margin_w`; at 2000 it deliberately exports 2 kW the car never gets |
| number Solar Minimum Amps | `min_a` | 5–32 | **5–16** | 1 | `start_watts = min_a × 240 + margin_w`; at 32 A that is 7,780 W against a measured 7.7 kW peak — the controller could never start (silent self-DoS), and grace burns budget at `min_a × 240` W |
| number Solar SoC Ceiling | `soc_ceiling` | 50–100 | **60–90** | 5 | 100 parks the pack at 100% every sunny day and the raise is sticky until restore; below 60 `raise_decision` can never exceed a normal limit, so the feature silently does nothing |
| number Solar Grace Budget | `grace_budget_wh` | 0–5000 | **0–600** | 50 | 5,000 Wh at $0.12 = $0.60 of import per dip, repeatedly, to avoid a restart worth ~$0.001; the measured compressor cycle needs 120–200 Wh |
| button Wake Tesla | — | — | — | — | 1 billed request; server-side 60 s rate limit **and** `solar.count_request()` so the cap actually sees it |

Every stored value in `car.db` today (`margin_w=100, min_a=5, soc_ceiling=90, grace_budget_wh=250.0`) falls inside these bounds and lands on the step — no entity boots showing a value it cannot represent.

### Not writable from HA, and why

| Field | Reason |
|---|---|
| `period_s` | The loop's clock: denominator of every hold timer, of the grace energy integral (`solar.py:374`), and of the daily request count. One slider silently rescales five behaviours and corrupts an in-flight grace accumulation. Setup page only |
| `watch_s` | Directly multiplies overnight spend; the collector already stands the watch down after dark because it cost ~$1.98/month. A slider undoes that fix |
| `view_refresh_ticks` | Looks like "make HA fresher", is "double the bill": at 1 with `period_s=120`, a 6 h charge adds 180 `vehicle_data` calls |
| `daily_request_cap` | A backstop you can raise from the screen that reports it tripping is the inverse of a backstop. Also **narrow `CONFIG_BOUNDS` to (0, 2000)** server-side and **reject with 400, never clamp** — a silent `min()` is the same class of bug as the silent `int()` coercion |
| `ramp_a`, `deadband_w`, `*_hold_s` | Safe within bounds but set-once; each is another YAML block to drift. Also, `*_hold_s` entities lie: the real wait is `period_s × (ceil(hold/period_s)+1)` except at 0, where it fires on the very next tick. Don't ship a number the owner will tune against a fiction |
| `import_rate` / `export_rate` | Display-only for our money block; the Energy Dashboard's static prices are the HA-side source of truth. Two editable tariffs on one site is a divergence generator |
| `deadline_soc` / `deadline_hour` | **Dead code.** Present in DDL, defaults, bounds and `setup.js`; read by nothing in the control path. Shipping them creates an automation the owner will trust that never fires |
| `garage_url`, home lat/lon/radius | See §2 |

---

## 4. ENERGY DASHBOARD WIRING

| Dashboard slot | Sensor | state_class | device_class / unit |
|---|---|---|---|
| Grid consumption | `sensor.site_grid_import` | total_increasing | energy / kWh |
| Return to grid | `sensor.site_grid_export` | total_increasing | energy / kWh |
| Solar production | `sensor.site_solar_production` | total_increasing | energy / kWh |
| Individual device: **Tesla** | `sensor.tesla_charge_energy_total` | total_increasing | energy / kWh |
| ↳ child, upstream = Tesla | `sensor.tesla_charge_energy_from_solar` | total_increasing | energy / kWh |
| ↳ child, upstream = Tesla | `sensor.tesla_charge_energy_from_grid` | total_increasing | energy / kWh |
| Import cost | — | — | static price `0.12` |
| Export compensation | — | — | static price `0.04` |

**`last_reset` is never sent.** It is ignored for `total_increasing` and is not even a valid key in the `rest:` sensor schema (`TEMPLATE_SENSOR_BASE_SCHEMA` is exactly `device_class`/`state_class`/`unit_of_measurement`, PREVENT_EXTRA) — a `last_reset:` line fails config validation.

**Why real `sensor.` entities and not injected external statistics:** `data.py::_reject_price_for_external_stat` refuses `number_energy_price` on an external statistic ID. Real entities keep the one-click static-price path and mean we never own the cost arithmetic.

### Counter-reset handling — the whole design of the `meters` table

`total_increasing`'s reset rule is one line (`sensor/recorder.py:475-493`): a drop below **90%** of the previous value = new cycle → `old_state = 0.0` → the *next* real sample is added in full, injecting the entire lifetime value into one 5-minute bucket. A drop between 90% and 100% is treated as a real *decrease* and silently subtracts. `EnergyCostSensor._update_cost` calls the same function, so a 5,000 kWh blip also books **$600** of cost in one step.

We have live vectors for both:
- `charged_solar_kwh`/`charged_grid_kwh` are recomputed per request by summing the whole `solar_ticks` table under `WHERE vin = ?`; `solar_routes._vin()` returns `""` on an empty `samples` table (cold start, restored `car.db` — and `deploy.sh:75` scps a snapshot), yielding a clean **200 with `0.0`**.
- A lifetime site figure via `period=lifetime` is revised downward by Tesla in the open current-year bucket — almost certainly under 10%, i.e. classified as a *dip*, a silent subtraction with only a log warning. Worse than a reset.

**Rule: a monotonic ratchet in SQLite, and `null` never `0`.**

1. New `meters` table: `(channel TEXT PRIMARY KEY, cumulative_wh REAL, last_closed_bucket_ts INTEGER, updated_ts INTEGER)`.
2. Six channels: `car_solar`, `car_grid`, `site_import`, `site_export`, `site_solar`.
3. Every update is `stored = max(stored, computed)` — forward-only. A restore, a VIN change, an empty-`samples` window or a Tesla downward revision **freezes** the counter instead of dropping it. Under-reporting during catch-up is the safe failure; a spike is the destructive one.
4. Site channels advance only on **completed** `calendar_history` buckets (day buckets strictly before today in `settings.timezone`) — never the open bucket, never a period-scoped recomputation, so there is no midnight/timezone coupling with HA.
5. If a channel's source is unknowable this poll, `/api/ha/meters` returns `null` for it and the HA availability template drops the sensor to `unavailable`. `recorder.py:225-240` silently discards non-float states from statistics, so `unavailable` is completely safe and `0` is destructive.
6. Both `DEMO` flags (`solar_routes.py:28` **and** `app.py:30`) make `/api/ha/meters` return **503**, not numbers. `demo.solar_status()` hardcodes `charged_solar_kwh: 214.6` and would permanently poison long-term statistics.

### Honest accuracy caveat (put this in the dashboard card text)

**Site figures are metered, not derived.** `energy.derive()` reads the Tesla gateway's own accumulated Wh registers (`solar_energy_exported`, `grid_energy_imported`, `grid_energy_exported_from_*`). They will not tie to the utility bill: different measurement point and accuracy class (site CTs ~±2%, no published class), the utility nets per its own interval and TOU rules, and its month is a meter-read date. *Agrees with the bill to within a few percent month over month; is not a substitute for it.*

**The car figures are ours, and are a lower bound.** `green.charged_split` is a left-endpoint rectangle rule over the collector's tick period, which backs off and takes 429s. Missed ticks drop energy outright (downward bias only); mid-tick amp ramps are attributed to the pre-ramp `car_w`; and `tick_solar_w`'s own docstring records that *"import is charged wholly against the car even though some of it fed the house."* **Plausibly 5–15% under true delivered energy, and the solar/grid split is an attribution convention, not a measurement** — there is no meter on that circuit. The "grid" half does **not** reconcile with the dashboard's grid-consumption figure and never will; that is why the hierarchy exists.

**Order matters in the UI:** all three car sensors must be added as individual devices *before* HA offers "upstream device" on the two halves. Without the hierarchy they sum to 2× the car and wrongly eat the untracked remainder.

---

## 5. FAILURE + COST BEHAVIOUR

### Three clocks, never mixed

`as_of` is `int(time.time())` at response time — a data-freshness lie, verified: a dev box with 2.9 days of no ticks still returns 200 with `as_of: now`. **No availability template may reference it.**

| Clock | Field | Gates | Threshold |
|---|---|---|---|
| Collector liveness | `collector_running` (bool, computed server-side) | **everything** | server-side: `heartbeat_age < max(300, 2×last_sleep_s + 60)` |
| Tick freshness | `last_tick_ts` | `surplus_w`, `amps` | `< 3 × period_s` |
| Snapshot freshness | `snapshot_age_s` | `soc`, `limit`, `range_mi`, `odometer`, `plugged_in`, `charging`, `at_home` | `< 3900` (2× `poll_asleep` + margin) |

The heartbeat is **new and mandatory**: `solar_ticks` gains a row only when a tick runs, and the collector returns early before any logging when `enabled == 0 and state == "idle"` (`collector.py:280`), writes no tick after dark (`:906`), and `poll_once` returns before `store.record` whenever `car_state != "online"` (`:82-84`). A 900 s tick-age threshold would mark every sensor unavailable all night, every night. Likewise `car_routes.py:221`'s existing `collector.running` keys on the *snapshot* with a 3600 s window — exported to HA it would fire "collector dead" every time the car sleeps an hour. Do not reuse that number; fix it.

### Availability template (exact form — the naive one is broken twice)

`rest` never calls `raise_for_status()`; on 401/403/500 `self.data` holds the error body and the entity stays **available**, feeding `{"detail": ...}` to the template. And `value_json.last_tick_ts` on a dict lacking the key is `Undefined`, for which `is not none` evaluates **True**. So:

```yaml
availability: >
  {{ value_json.get('schema') == 1
     and value_json.get('collector_running')
     and value_json.get('last_tick_ts') is number
     and (as_timestamp(now()) - value_json.last_tick_ts) < 360 }}
```

`schema: 1` is returned by `/api/ha/*` and is the cheap guard against an auth failure or an error body being parsed as data.

### What HA shows

| Condition | We return | HA shows |
|---|---|---|
| Mini down / rebooting | connection refused / timeout | all entities **unavailable** (`rest.data is None`) |
| Wrong / rotated API token | 401 JSON body | all entities **unavailable** — via the `schema == 1` guard, not by luck |
| Collector dead, app alive | 200, `collector_running: false` | all entities **unavailable** |
| Car asleep | 200, `snapshot_age_s` growing | SoC/range/plugged/at-home unavailable past 3900 s; `Tesla Data Age` and all controller entities stay live |
| Controller idle / dark / disabled | 200, stale `last_tick_ts` | Surplus and Amps unavailable; State, flags and energy meters stay live |
| Tesla 429 | `rate_limited: true`, `backoff_s > 0` | `problem` binary_sensor on; tick-derived values age out naturally |
| Daily cap tripped | `capped: true` | `problem` binary_sensor on |
| Signing proxy down | commands 502 | Wake button errors loudly; every sensor unaffected (reads go direct) |
| ratgdo unreachable | `{"reachable": false}` | cover **unavailable** — never "closed" |
| Meters source unknown | `null` for that channel | that energy sensor **unavailable**, dropped from statistics, no spike |

Both restart paths are clean: HA restart re-fetches with no carried state; a mini reboot has launchd bring all agents back and entities flap unavailable → available on their own.

### The cost rule

**HA touches `/api/ha/state` and `/api/ha/meters` and nothing else, forever.** Both are pure SQLite. Steady-state Tesla cost of the entire integration: **zero requests.**

Enforced three ways:
1. `ha_routes.py` never imports the Fleet client (unit-tested).
2. `/api/car/state`, `/api/car/health`, `/api/dashboard`, `/api/car/wake` are documented off-limits and are the *only* reason anyone would be tempted to point HA elsewhere — `/api/ha/state` carries the snapshot fields (`plugged_in`, `charging_state`, `range_mi`, `odometer`) that made `/api/car/state` attractive.
3. **`daily_request_cap` is made real.** Today it is bumped only at `collector.py:284`; nothing in `car_routes.py` or `app.py` consults it, so a wall tablet on `/api/car/state` spends 5,760 requests/day entirely outside the cap while `requests_today` reads a comfortable 77/400. `POST /api/car/wake` and `POST /api/car/command/{id}` must call `solar.count_request()` and refuse when capped.

Separately: `car_routes._vin()` is `client.resolve_vin()`, and `TESLA_VIN` is **not in `.env`** — so it falls through to `GET /api/1/vehicles` and every row of the naive cost table is off by one. Set `TESLA_VIN`; that fixes `/history`, `/state`, `/health` and `/wake` in one line.

---

## 6. IMPLEMENTATION TASKS

**Task 1 — API token + CSRF middleware. Ships alone; fixes a live hole.**
*Files:* new `auth.py`, wire in `app.py` above the router includes. `.env` gains `API_TOKEN`; `deploy-macos.sh` propagates it.
*Does:* (a) if `request.client.host` not in `{127.0.0.1, ::1}`, require `X-Api-Key == API_TOKEN` via `secrets.compare_digest` → 401. (b) **on every mutating method regardless of source IP**, reject when `Sec-Fetch-Site` is present and not `same-origin` → 403. Part (b) is not optional: the drive-by CSRF form targets `127.0.0.1:8000` from the owner's *own* browser, so the loopback exemption alone does not close it. (c) `/api/ha/*` requires the header even from loopback.
*Verify:* from another LAN host `GET /api/car/home` → 401 and `POST /api/car/garage/open` → 401; from the mini → 200; a `curl -H 'Sec-Fetch-Site: cross-site' -X POST .../api/car/wake` → 403; the local browser UI still works end to end. Add tests in `tests/`.
*Note in `SETUP.md`:* uvicorn 0.34.0 defaults `proxy_headers=True` and is safe only because `forwarded_allow_ips` falls back to `127.0.0.1`. **If anything ever fronts `:8000` over loopback (cloudflared, nginx), every request arrives as 127.0.0.1 and the loopback exemption whitelists the internet.**

**Task 2 — close the cost leaks.** *Files:* `.env` (+`deploy-macos.sh`), `car_routes.py`.
Set `TESLA_VIN`; add `solar.count_request()` + cap refusal to `/wake` and `/command/{id}`; add a 60 s server-side rate limit on `/wake`.
*Verify:* `requests_today` increments after a wake; a second wake inside 60 s → 429; `/api/car/history` issues zero Tesla calls (assert on the client mock).

**Task 3 — collector heartbeat.** *Files:* `store.py` (DDL + accessors), `collector.py` (write at the top of every loop iteration, before every `continue`/`return`, recording `sleep_s`), `solar_routes.py` unchanged.
*Verify:* stop the collector; within `2×sleep_s + 60` the computed `collector_running` flips false. `enabled=0` + car asleep still writes heartbeats.

**Task 4 — `meters` table + ratchet + car channels.** *Files:* `store.py` (DDL), new `meters.py` (`advance(channel, computed_wh)` = forward-only max; `read_all()`), `collector.py` calls `advance` for `car_solar`/`car_grid` each tick.
*Verify:* unit test — feed 5000, then 0, then 5000 → stored stays 5000, never returns 0; feed `None` → channel reads `null`.

**Task 5 — `GET /api/ha/state` and `GET /api/ha/meters`.** *Files:* new `ha_routes.py`, included in `app.py` **above** the static catch-all mount.
*Does:* DB-only. Uses the SQLite `_vin()` pattern, not `resolve_vin()`. Returns `schema: 1`, the three clocks, `collector_running`, controller state/flags, the snapshot `view` subset, `classification`, and (meters route) the six channels in kWh or `null`. Returns **503** if either `DEMO` flag is set.
*Verify:* `pytest` asserting `ha_routes` imports no Tesla client; curl each endpoint and diff the key set against the YAML templates; `DEMO=1` → 503; `X-Api-Key` missing → 401.

**Task 6 — HA phase 1 YAML (read-only).** *Files:* servy `/config/configuration.yaml`, `/config/secrets.yaml`.
Two `rest:` resources with `headers: {X-Api-Key: !secret tesla_api_key}`, `timeout: 10`, all Group A/B/C sensors and binary_sensors with the `schema == 1` availability templates.
*Verify:* HA restart, Developer Tools → States shows every entity with a real value; unplug the mini's app → all go unavailable within one scan; **confirm `availability:` is accepted on `rest:` sensors in 2026.7.4** (verified in core `dev`, undocumented on the integration page — this is the one thing to check at first restart).

**Task 7 — controls.** *Files:* `solar_routes.py` (`CONFIG_BOUNDS["daily_request_cap"] → (0, 2000)`, reject-not-clamp), servy `configuration.yaml` (`rest_command:` one per field, `template:` switches and numbers with the §3 bounds, `| int`/`| float` casts, `optimistic: true` on switches, `update_entity` chasers).
*Verify:* move each slider, confirm the value in `car.db` and that HA re-reads the stored value; send an out-of-bounds value by hand → 400 and the entity snaps back.

**Task 8 — site meters ingest + Energy Dashboard.** *Files:* `collector.py` (hourly `calendar_history` day-bucket ingest, closed buckets only, `settings.timezone`), `meters.py`, `ha_routes.py`.
*Cost budget, stated:* 24 Tesla energy calls/day, energy-product APIs are free but not unmetered (the collector already logs `live_status` 429 backoffs) — share the existing backoff.
*Verify:* run twice in an hour, confirm the counter does not move for today's open bucket; simulate a downward-revised bucket and confirm the ratchet holds; then configure the six Energy Dashboard slots and check HA's cost figure matches `energy.money()` to rounding — divergence means something is mis-wired.

**Task 9 — garage cover. Blocked on the owner's Local Network grant.** *Files:* `solar_routes.py` (new `POST /api/car/garage/close_unattended` running the scheduled-close discipline: `safe_to_close()` gate, light on, `garage_close_warn_s`, re-read, abort on obstruction or on the door no longer `Open`; plus `POST /api/car/garage/rearm`), `configuration.yaml` (`template: cover:` `device_class: garage` + `binary_sensor` obstruction `device_class: problem` + `switch.garage_auto_open`).
The cover's CLOSE routes to the **unattended** path, never to `POST /api/car/garage/close` — that endpoint is documented as "the owner is present and just pressed it," and an MQTT/automation close is not a person standing there. Any `door_state` outside the exact-match set, or `reachable: false`, maps to **unavailable** — never to `closed`. `OPEN`/`CLOSE` only; no stop.
*Verify:* re-probe `GET /api/car/garage` after the grant. **Note the evidence conflict:** `collector.log:613` shows a successful ratgdo read on 2026-07-28 while a later probe returned `reachable: false`. Re-probe before concluding either way; do not ship a permanently-unavailable cover.

**Task 10 — remove the dead config.** *Files:* `solar.py` (DDL/defaults), `solar_routes.py` (`CONFIG_BOUNDS`, `NULLABLE_CONFIG_FIELDS`), `static/setup.js`, tests. Delete `deadline_soc`/`deadline_hour`, or implement them in the collector. Do not export either way.

---

## 7. WHAT THE OWNER MUST DO

**Before anything HA-facing (blocking):**
1. On the Mac mini: generate a token (`openssl rand -hex 32`), add `API_TOKEN=…` to `/Users/d/Code/tesla_automation/.env`, run `./deploy-macos.sh`, confirm the app agent restarted.
2. Generate a **second, different** token value for HA and add it to `.env` as `API_TOKEN_HA` (rotatable independently of the browser UI's).
3. Verify from a laptop: `curl http://10.0.0.84:8000/api/car/home` → **401**. If it returns 200, stop — the middleware is not live and the garage/coordinates hole is still open.

**Home Assistant (servy) — all of this is text edits plus one restart; nothing compiles, nothing new runs:**
4. Create `/config/secrets.yaml` (if absent) with `tesla_api_key: <API_TOKEN_HA>`. Ensure it is excluded from any config sharing.
5. Paste the `rest:` block into `/config/configuration.yaml` (currently 265 bytes — it becomes a real file that now matters).
6. **Restart HA once.** Then Developer Tools → States: confirm every entity exists and none reads `unknown`. Specifically confirm the `availability:` key was accepted on `rest:` sensors — if HA logs an unknown-key error, drop `availability:` and gate inside `value_template` instead.
7. Paste the `rest_command:` and `template:` blocks (task 7), restart again.
8. Assign all entities to an area and rename to taste. **There is no device card** — YAML REST/template entities never get a device-registry entry. This is the accepted cost of the mechanism.
9. Energy Dashboard, in this order: add Grid consumption (`sensor.site_grid_import`), Return to grid (`sensor.site_grid_export`), Solar production (`sensor.site_solar_production`); set static prices **0.12** import and **0.04** export; add all **three** Tesla energy sensors as individual devices; **then** set the solar and grid halves' upstream device to `Tesla Charge Energy Total`. The upstream option is not offered until all three are already listed.
10. Confirm the `backup` integration (already configured) is capturing `/config` — `configuration.yaml` is now load-bearing.

**Only needed for the garage (task 9):**
11. On the Mac mini: System Settings → Privacy & Security → **Local Network** → enable the Python interpreter. Two gotchas: the row often does not appear until the binary has *attempted* a LAN connection, so trigger a garage read first and then look; and the grant lands on the **binary** (`/Library/Frameworks/Python.framework/.../Python`), so it grants every script that interpreter runs.
12. Re-run `curl -H 'X-Api-Key: …' http://10.0.0.84:8000/api/car/garage` and confirm `reachable: true` before pasting the cover YAML.

**Optional:**
13. HomeKit Bridge, **via the UI only** (a UI-created bridge must be configured in the UI; mixing with YAML yields two bridges on different ports). Export `switch.solar_charging`, `sensor.tesla_battery`, and — after task 9 — `cover.garage_door`.

**Do not do, ever:**
14. Do not install the `tesla_fleet` integration. It would be a second uncoordinated commander with dumb `number.charge_current` / `switch.charge` setpoints that know nothing of our hysteresis, `grace_s`, `raised_to` restore or `dirty` flag, and a second poller on an energy site that already 429s us.
15. Do not add the MQTT integration for anything. HA gets exactly one broker, permanently; if the ALPR camera feed is ever wanted in HA, that slot must go to HiveMQ Cloud.
16. Do not point any HA `rest:` resource, `rest_command:`, automation or dashboard card at `/api/dashboard`, `/api/car/state`, `/api/car/health` or `/api/car/wake`. Those are the billed endpoints, and `daily_request_cap` does not protect them today.