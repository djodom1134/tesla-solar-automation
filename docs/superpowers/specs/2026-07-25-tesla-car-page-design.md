# Tesla car page — design

Date: 2026-07-25
Status: approved, ready for implementation planning

Adds a vehicle page to the existing Tesla dashboard (which today shows solar
import/export). Shows charge state, live location on a map, a state-of-charge
graph with selectable timeframes, and the full set of remote controls.

Target vehicle: VIN `5YJSA00000F000000` — 2022 Model S, display name "Roadrunner",
NA region, `command_signing: "required"`.

---

## 1. What the Fleet API can and cannot do

Established by research against Tesla's docs, the `teslamotors/vehicle-command`
source, and the account's own `/api/1/products` response.

| Requested | Verdict |
|---|---|
| Charge state | Yes. `GET /api/1/vehicles/{vin}/vehicle_data?endpoints=charge_state`. Plain bearer REST, no signing. |
| Cameras | **No.** No endpoint returns video, clips, stills, or thumbnails — not scope-gated, not beta; they do not exist. Sentry Live Camera is end-to-end encrypted, so Tesla cannot relay it. Dashcam footage is USB-local. |
| Location on a map | Yes. `endpoints=drive_state;location_data`, requires the `vehicle_location` scope in addition to `vehicle_device_data`. |
| Charge on/off | Yes, but requires signed commands. Charging commands are **not** exempt from signing. |
| All other controls | Yes, ~70 commands, all signed. |
| SoC graph | Yes, **but there is no historical SoC anywhere in the Fleet API.** No vehicle analogue to the energy `calendar_history` endpoint. History must be collected locally and starts the day the collector starts. Prior months are unrecoverable. |

### Camera substitute

Since no imagery is available, the page surfaces the camera-adjacent *state* that
does exist, and says plainly why there is no video:

- `vehicle_state.sentry_mode`, `sentry_mode_available`
- `vehicle_state.dashcam_state` (e.g. `"Recording"`), `dashcam_clip_save_available`
- A Sentry Mode on/off control (signed command)

Note: the `vehicle_cmds` scope description on Tesla's own site says "access Live
Camera". That is first-party app wording with no corresponding public endpoint.
It must not be treated as a roadmap item.

---

## 2. Scopes and re-authorization

Current grant: `openid offline_access energy_device_data`.

Target grant:

```
openid offline_access energy_device_data
vehicle_device_data vehicle_location vehicle_cmds vehicle_charging_cmds
```

`location_data` is **not** a scope — it is a value of the `endpoints=` query
parameter. Both the `vehicle_location` scope and the `location_data` endpoints
value are required; missing either silently strips coordinates rather than
returning 403.

Re-auth is a two-place change:

1. developer.tesla.com → app → Credentials & API → API & Scopes → enable the new
   scopes. Propagation takes up to ~10 minutes; testing earlier produces a
   phantom failure.
2. Re-run the OAuth flow with the widened scope string and
   `prompt_missing_scopes=true`.

Not required: re-registering the partner account, regenerating or re-hosting the
public key. The `example.com` NA registration stays valid. The key must remain
hosted at `/.well-known/appspecific/com.tesla.3p.public-key.pem` permanently —
the Tesla app re-fetches it at pairing time.

**Token hazard.** A new grant invalidates the previous refresh token. If re-auth
half-fails, the working solar dashboard breaks. `TokenStore.save` will therefore
copy the existing token file to `.tokens.json.bak` before overwriting.

---

## 3. Command signing

Reads are ordinary authenticated GETs. Writes are not: the car ignores commands
that are not cryptographically signed by a key it has been introduced to. Tesla's
cloud cannot sign on the owner's behalf.

Setup, in order:

1. Re-auth with `vehicle_cmds` + `vehicle_charging_cmds` (§2). Prerequisite for step 2.
2. Pair the virtual key: open `https://tesla.com/_ak/example.com` on the phone,
   confirm in the Tesla app (4.27.3+). Car must be online. Cars cap at 20 keys.
3. `brew upgrade go` (need 1.23+, currently 1.19.1), then build
   `tesla-http-proxy` from `github.com/teslamotors/vehicle-command`.
4. Generate a self-signed TLS cert for the proxy (secp384r1, `CN=localhost`) —
   TLS is mandatory and cannot be disabled.
5. Run the proxy on `localhost:4443` with `-key-file keys/private-key.pem`.

The signing key **must** be the same pair whose public half is hosted at
`example.com`. `keys/private-key.pem` is already `prime256v1` (NIST P-256) and
already registered, so it is reused directly. No new keys are generated.

The proxy takes the OAuth bearer token from the incoming `Authorization` header
and forwards it, so the backend sends the same header it already uses.

Because the proxy's TLS certificate is self-signed, the backend's HTTP client
must be given that certificate explicitly as its CA bundle for proxy requests
only. TLS verification is never disabled — the direct Fleet API client keeps
normal verification, and the command client trusts exactly one extra cert.

### Routing decision

**Only `/api/1/vehicles/{vin}/command/*` goes through the proxy. Everything else
goes direct to the Fleet API.**

Tesla recommends routing all traffic through the proxy so callers need one code
path. This design deliberately does not, so that proxy downtime degrades only the
buttons — the solar dashboard, all vehicle reads, and the SoC collector keep
working. The cost is one branch in the client, which is trivial.

### Commands that work without signing

Useful for graceful degradation: `wake_up` (not under `/command/`, forwarded
verbatim), and exactly four command endpoints that route to plain REST —
`navigation_request`, `set_managed_charge_current_request`,
`set_managed_charger_location`, `set_managed_scheduled_charging_time`.

### Never trust HTTP 200

A car can reject a command with
`200 {"response":{"result":false,"reason":"unsigned_cmds_hardlocked"}}`. Every
command response is checked for `response.result` and `response.reason`, and
known reasons (`already_set`, `is_charging`, `not_charging`, `disconnected`,
`car_wash`, `not_supported`) map to human-readable text.

---

## 4. Architecture

### Backend modules

| File | Responsibility |
|---|---|
| `vehicle.py` | Normalize raw `vehicle_data` into a view model. Pure functions, fixture-testable. Mirrors the role of `energy.py`. |
| `store.py` | SQLite. SoC samples, last-known snapshot, bucketed history queries. |
| `collector.py` | Adaptive poller. Runs standalone (`python collector.py`), installed under launchd. |
| `commands.py` | Command catalog (id, label, group, risk, params, scope) + dispatch through the proxy + result interpretation. |
| `car_routes.py` | `APIRouter` for `/api/car/*`, keeping `app.py` focused. |

Extended: `tesla.py` gains `vehicles()`, `vehicle(vin)`, `fleet_status(vins)`,
`vehicle_data(vin, endpoints)`, `wake_up(vin)`, `command(vin, name, payload)`.
`config.py` gains scopes, proxy settings, poll intervals, and the DB path.

### Frontend

The existing `static/app.js` holds ~400 lines of chart machinery the car page
needs. Duplicating it would be worse than extracting it, so:

- `static/chart.js` — SVG primitives: `chartFrame`, `niceTicks`, `barPath`,
  tooltip, `attachCrosshair`, `renderLines`, `renderLegend`
- `static/shared.js` — `api()`, formatters, theme toggle, gate rendering, `el()`
- `static/app.js` — solar page, importing the above
- `static/car.js` — car page
- `static/car.html` — car page markup
- `static/vendor/leaflet.{js,css}` — vendored, no CDN dependency at runtime
- Solar ⇄ Car nav in the topbar of both pages

The extraction is behavior-preserving and verified against `DEMO=1` before any
car code is written.

Map tiles come from `tile.openstreetmap.org` (keyless, attribution required).
This is the one runtime network dependency outside the Fleet API.

### Endpoints

```
GET  /api/car/state              → snapshot + age + online state + capabilities
POST /api/car/wake               → explicit wake, never automatic
GET  /api/car/history?range=…    → 24h | 7d | 30d | 90d | all
POST /api/car/command/{id}       → body = params; returns interpreted result
GET  /api/car/health             → scopes ok? key paired? proxy up? collector alive?
GET  /api/car/commands           → the command catalog, so the UI is data-driven
```

---

## 5. The four render states

The car is asleep most of the time. Every element on the page renders against
exactly one of these, and which one is always visible to the user:

1. **Live** — car online, data fresh from this request
2. **Last known** — asleep/offline: stored snapshot with a prominent "as of 3h
   ago" stamp and a Wake button
3. **Needs setup** — missing scope, unpaired key, or proxy down, each stating the
   specific fix
4. **Empty** — no history collected yet, showing the collection start date

**Never auto-wake.** `wake_up` costs ~10× a read, is capped at 3/min, and drains
the battery. Waking is always an explicit user action. If a control is clicked
while the car is asleep, the confirm step offers to wake first.

---

## 6. Layout

- **Hero** — SoC ring, range, charging status, time-to-full
- **Live tiles** — charge power, charge limit, range, inside/outside temp, odometer
- **Map card** — marker with heading; speed and navigation destination when moving
- **SoC chart** — 24h / 7d / 30d / 90d / All
- **Controls** — four collapsible groups: Charging, Climate & comfort,
  Access & security, More
- **Status footer** — last update, API calls today, month-to-date cost estimate

Reuses the existing CSS custom properties and the validated chart palette. Dark
mode inherits from the existing theme toggle.

### Control groups

| Group | Commands | Confirm? |
|---|---|---|
| Charging | charge start/stop, set limit %, set amps, charge port open/close | no |
| Climate & comfort | climate on/off, set temps, seat heaters, wheel heater, defrost | no |
| Access & security | lock, unlock, front/rear trunk, sentry on/off, flash lights, honk | unlock, trunk |
| More | windows vent/close, valet mode, speed limit, media, software update, homelink | windows, valet |

Known API quirks handled explicitly:

- Seat and steering-wheel heat commands fail unless climate is already on — the
  UI disables them with an explanatory tooltip until climate is running.
- Eight documented commands return HTTP 400 `invalid_command` through Tesla's own
  proxy (`sun_roof_control`, `navigation_gps_request`, `navigation_sc_request`,
  `navigation_waypoints_request`, `upcoming_calendar_entries`,
  `remote_steering_wheel_heat_level_request`,
  `remote_auto_steering_wheel_heat_climate_request`, `remote_boombox`). These are
  excluded from the catalog rather than shipped broken.
- `window_control`'s `lat`/`lon` are a user-proximity proof rather than a
  target, and Tesla's own proxy ignores them entirely — so they are omitted
  rather than faked.
- Commands take the 17-character VIN, never the numeric Fleet API id.

---

## 7. SoC history

### Storage

SQLite at `car.db` (gitignored, alongside `.tokens.json`).

Two processes write to it: the launchd collector and the FastAPI app (which
records a free sample whenever `/api/car/state` fetches fresh data anyway). The
database is therefore opened in WAL mode with a busy timeout, and every write is
an idempotent `INSERT OR REPLACE` keyed on `ts` rounded to the second, so a
collector poll and a page load landing in the same instant cannot conflict or
double-count.

```sql
CREATE TABLE samples (
  ts                   INTEGER PRIMARY KEY,  -- epoch seconds
  vin                  TEXT NOT NULL,
  battery_level        INTEGER,
  usable_battery_level INTEGER,
  charge_limit_soc     INTEGER,
  charging_state       TEXT,
  charger_power        INTEGER,
  charge_energy_added  REAL,
  est_battery_range    REAL,
  odometer             REAL,
  inside_temp          REAL,
  outside_temp         REAL,
  latitude             REAL,
  longitude            REAL,
  shift_state          TEXT,
  car_state            TEXT                  -- online|asleep|offline at sample time
);

CREATE TABLE snapshot (
  vin  TEXT PRIMARY KEY,
  ts   INTEGER NOT NULL,
  json TEXT NOT NULL                          -- full last-good vehicle_data
);
```

`battery_level` and `usable_battery_level` are different numbers and diverge in
cold weather. The chart plots `battery_level`; `usable_battery_level` appears in
the tooltip. Plotting both would read as a rendering bug.

### Collection

Adaptive, cost-aware, and gated on the free state check:

```
loop:
  state = GET /api/1/vehicles/{vin}          # free
  if state != online:
      sleep asleep_interval                   # no paid call
      continue
  data = GET vehicle_data(...)                # paid
  store sample
  sleep by mode: driving | charging | idle
```

Default intervals, all configurable in `.env`:

| Mode | Interval |
|---|---|
| Driving (`shift_state` not null/`P`) | 2 min |
| Charging | 5 min |
| Online, idle | 15 min |
| Asleep / offline | 5 min, free state check only |

That is roughly 110 paid calls/day ≈ $6/month against the $10 credit. The page's
own fetches record samples opportunistically at no extra cost.

Notes:
- 408 responses are billed. Never poll a sleeping car for data.
- On 2021+ vehicles with firmware 2023.11+, polling does not prevent sleep — only
  commands keep the car awake. So this collector does not cause vampire drain.
- Rate limits are per-account and shared with any other authorized app
  (TeslaMate, Tessie, Home Assistant): 60/min realtime data, 3/min wakes,
  30/min commands.

Fleet Telemetry was evaluated and rejected: it needs a self-hosted Go server on a
public FQDN under `example.com` terminating mutual TLS directly (no reverse
proxy or tunnel possible), plus a 24/7 machine. Worth revisiting only if an
always-on VPS on that domain appears.

### Rendering gaps honestly

A sleeping car produces no samples. The chart draws a **solid** line across
sampled regions and a **faded dashed** segment across sleep gaps, so a gap reads
as `80% → (dashed 8h) → 78%` rather than as either a hole or a fabricated slope.

Long ranges are downsampled server-side by SQL bucketing (last value per bucket,
plus min/max for an optional band), so a 90-day query never ships 100k points.

### Deployment

launchd agent at `~/Library/LaunchAgents/com.example.tesla-collector.plist`,
`RunAtLoad` + `KeepAlive`, logging to a file the health endpoint can stat. The
collector runs independently of the FastAPI server so history is continuous even
when the dashboard is closed.

---

## 8. Billing

Owner's position: no payment method should be needed, on the basis that
personal use of one's own vehicle falls under Tesla's free/discounted tier.
That is very likely right — Tesla flags qualifying vehicles with
`discounted_device_data` — so billing setup is **not** a prerequisite and does
not block any task.

It is instead treated as something to verify rather than assume:

- The moment vehicle scopes are live, `POST /api/1/vehicles/fleet_status`
  reports `discounted_device_data` for this VIN. `setup_tesla.py doctor` prints
  it, so the answer comes from the account rather than from documentation.
- The collector's defaults stay conservative regardless. Cheap polling costs
  nothing extra if the tier is free, and protects the solar dashboard if it
  is not.
- The page footer shows calls today and a month-to-date estimate, labeled an
  estimate, so real usage can be reconciled against the portal.

The reason this is worth verifying rather than waving through: if the account
does turn out to be metered and the limit is exceeded, Tesla disables the whole
app — which would take the working solar dashboard down with it. One field in
one response settles it.

---

## 9. Demo mode

`DEMO=1` already serves synthetic solar data through the real derivation path.
`demo.py` is extended with synthetic `vehicle_data` and a synthetic SoC history
including sleep gaps, a charging ramp, and a drive.

This is not a nicety: the car is offline most of the time and real history starts
empty, so demo mode is the only way to build and verify the page without burning
API calls or waking the car.

---

## 10. Error and edge cases

| Case | Handling |
|---|---|
| 408 from `vehicle_data` while online | Normal, not an error. Retry once, then fall back to last-known. |
| Car asleep/offline | Last-known snapshot + age stamp + Wake button. |
| Proxy down | Controls disabled with the start command shown. Reads unaffected. |
| Key not paired | Controls disabled, pairing link shown. Detected via `fleet_status.key_paired_vins`. |
| Missing scopes | Affected sections show a re-auth button. |
| 429 rate limited | Surfaced plainly, including that limits are shared with other Tesla apps. |
| Command 200 but `result:false` | Show the mapped `reason`. |
| Empty history | "Collecting since <date>" empty state. |
| Collector not running | Health endpoint reports it; footer shows a warning. |

---

## 11. Testing

Pure logic gets unit tests (pytest, new to this project, kept light):

- `vehicle.py` — derivation from recorded fixture JSON
- `store.py` — bucketing and gap detection at range boundaries
- `commands.py` — parameter validation, reason mapping
- `collector.py` — `next_interval(state)` as a pure function

Browser verification runs against `DEMO=1` across light and dark, all SoC ranges,
and every render state. Live-car verification is manual and explicitly sequenced
after the scope upgrade.

---

## 12. Build order

Deliberately sequenced so the collector starts gathering history before the UI
that displays it exists, and so nothing risky happens before the safety net.

1. Scope widening in the portal (manual, user — **done 2026-07-25**) +
   `config.py` scope change
2. Token backup, then re-auth. Immediately after, `fleet_status` confirms
   `discounted_device_data` and `vehicle_command_protocol_required`
3. `brew upgrade go` (**done 2026-07-25**)
4. `tesla.py` vehicle reads + `vehicle.py` + `store.py`
5. `collector.py` + launchd install — **history starts accruing here**
6. Frontend extraction (`chart.js`, `shared.js`), verified against DEMO
7. `car.html` / `car.js` read-only: hero, tiles, map, SoC chart
8. Proxy setup: build `tesla-http-proxy`, generate TLS cert, pair virtual key
9. `commands.py` + controls UI
10. `setup_tesla.py doctor` extensions, `SETUP.md` / `README.md` updates

---

## 13. Open items requiring live verification

Research surfaced genuine contradictions. Each is resolved empirically against
the real account rather than guessed:

- Whether `wake_up` needs `vehicle_device_data` or `vehicle_cmds`. Both are being
  requested, so this cannot block, but the doctor should report which is true.
- Whether the existing refresh token survives scope widening. Tesla's docs say
  yes, community reports say no. The backup makes either outcome safe.
- Whether this account is billed at all. Expected free via
  `discounted_device_data`; confirmed from `fleet_status` rather than assumed.
  The footer's cost estimate is labeled an estimate and reconciled against the
  portal's usage view.
- Timezone: `.env` says `America/Los_Angeles`; last session's evidence hinted at
  Mountain. The car's own coordinates will settle it once location is live.
