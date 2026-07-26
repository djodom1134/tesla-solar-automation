# Tesla Car Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a vehicle page to the existing Tesla dashboard showing charge state, live location on a map, a state-of-charge graph with selectable timeframes, and the full set of signed remote controls.

**Architecture:** FastAPI backend gains vehicle read modules (`vehicle.py` normalizer, `store.py` SQLite, `collector.py` launchd poller) and a signed-command path through Tesla's `tesla-http-proxy`. The vanilla-JS frontend gains a second page reusing chart primitives extracted from the existing solar page. Reads go direct to the Fleet API; only `/command/*` traverses the proxy, so proxy downtime degrades buttons only.

**Tech Stack:** Python 3.13, FastAPI, httpx, SQLite (stdlib `sqlite3`), pytest. Vanilla ES modules, hand-rolled SVG charts, Leaflet 1.9.4 (vendored) + OpenStreetMap tiles. `tesla-http-proxy` v0.4.1 (Go, already built to `~/go/bin`).

## Global Constraints

- **Source of truth for every JSON key is `docs/tesla-field-reference.md`.** Do not invent key names. Read the relevant section before writing any field access.
- **Use `.get()` for every vehicle field.** Whole-key absence is the dominant failure mode, not null. Hardware-gated and location keys are omitted entirely.
- **Units on the wire are fixed and mixed:** distances miles, speed mph, tire pressure bar, all temperatures Celsius. `gui_settings` is a display preference and never changes the payload.
- **Timestamps are mixed:** epoch **milliseconds** for `*.timestamp`; epoch **seconds** for `gps_as_of`, `scheduled_*_time`, `tpms_last_seen_*`.
- **Never trust HTTP 200 on a command.** Parse `response.result`; `error` is an empty string on success, never null.
- **Never auto-wake the car.** `wake_up` is ~10× a read, capped 3/min, and drains the 12V.
- **Commands take the 17-character VIN**, never the numeric Fleet API id — the proxy 404s otherwise.
- **`endpoints` is mandatory** on `vehicle_data` and semicolon-separated (`%3B`). Omitting a group silently omits the whole object.
- **HTTP 408 means asleep** and has no body at all.
- Region NA: `https://fleet-api.prd.na.vn.cloud.tesla.com`. Token exchange at `https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token`.
- Vehicle under test: VIN `5YJSA1E5XNF477026`, 2022 Model S "Stallion", `command_signing: required`.
- Existing solar dashboard must keep working at every step. It is the regression bar.

---

### Task 0: Initialize the repository

The project is not under version control and this change is large. Every later task ends in a commit, which requires this.

**Files:**
- Create: `.gitignore` (extend existing)

- [ ] **Step 1: Confirm we are not already in a repo**

Run: `git -C /Users/d/Code/tesla_automation rev-parse --is-inside-work-tree 2>&1`
Expected: `fatal: not a git repository ...`

- [ ] **Step 2: Verify secrets are ignored before the first commit**

Read the existing `.gitignore`. It must contain `.env`, `.tokens.json`, and `keys/`. Append anything missing, plus the new artifacts:

```
car.db
car.db-wal
car.db-shm
.tokens.json.bak
keys/tls-key.pem
keys/tls-cert.pem
```

- [ ] **Step 3: Initialize and make the first commit**

```bash
cd /Users/d/Code/tesla_automation
git init
git add -A
git status --short
```

Inspect `git status --short` output and confirm **none** of `.env`, `.tokens.json`, `keys/private-key.pem`, or `car.db` are staged. If any are, fix `.gitignore` and `git rm --cached` them before continuing.

```bash
git commit -m "chore: initial commit of solar dashboard"
```

- [ ] **Step 4: Verify**

Run: `git log --oneline && git ls-files | grep -E '^\.env$|tokens|private-key' ; echo "exit=$?"`
Expected: one commit listed, and the grep matches nothing (`exit=1`).

---

### Task 1: Widen OAuth scopes and re-authorize

**Files:**
- Modify: `config.py:25` (the `SCOPES` list)
- Modify: `tesla.py:82-88` (`TokenStore.save`), `tesla.py:128-139` (`authorize_url`)

**Interfaces:**
- Consumes: nothing.
- Produces: `config.SCOPES` includes the four vehicle scopes; `.tokens.json` grants them. Every later task depends on this.

- [ ] **Step 1: Back up the working token by hand before touching anything**

```bash
cd /Users/d/Code/tesla_automation
cp .tokens.json .tokens.json.manual-backup
ls -la .tokens.json*
```

This is belt-and-braces. Step 3 automates it, but if the automation is wrong we still have a copy of the grant that currently powers the solar dashboard.

- [ ] **Step 2: Widen the scope list**

In `config.py`, replace the `SCOPES` definition:

```python
# Read-only energy access plus vehicle reads and commands.
# openid+offline_access are what get us a refresh token.
# `vehicle_location` is the scope; `location_data` is a value of the
# vehicle_data `endpoints` param — both are required for coordinates.
SCOPES = [
    "openid",
    "offline_access",
    "energy_device_data",
    "vehicle_device_data",
    "vehicle_location",
    "vehicle_cmds",
    "vehicle_charging_cmds",
]
```

- [ ] **Step 3: Make token saves keep a backup**

In `tesla.py`, replace the body of `TokenStore.save`:

```python
    def save(self, tokens: Tokens) -> None:
        # A new grant invalidates the previous refresh token. If a re-auth
        # half-fails we want the old grant on disk to fall back to.
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tokens.to_dict(), indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)  # atomic on POSIX
        self._tokens = tokens
        self._loaded = True
```

Add `import shutil` to the imports at the top of `tesla.py`.

- [ ] **Step 4: Ask Tesla to prompt for the newly-added scopes**

In `tesla.py`, in `authorize_url`, add `prompt_missing_scopes` to the params dict:

```python
    def authorize_url(self, state: str) -> str:
        params = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "scope": " ".join(SCOPES),
                "state": state,
                "prompt": "login",
                # Without this, an account that already consented to the old
                # scope set is silently re-issued a token missing the new ones.
                "prompt_missing_scopes": "true",
            }
        )
        return f"{AUTH_BASE}/oauth2/v3/authorize?{params}"
```

- [ ] **Step 5: Restart the server and re-authorize**

```bash
lsof -ti:8000 | xargs kill -9 2>/dev/null
cd /Users/d/Code/tesla_automation && .venv/bin/python app.py > /tmp/app.log 2>&1 &
sleep 2 && curl -s localhost:8000/api/config
```

Then open http://localhost:8000, click Disconnect, then Connect, and complete the Tesla login. **The user must do this interactively.** Do not restart the server between the click and the callback — the CSRF `state` lives in memory.

- [ ] **Step 6: Verify the new scopes actually landed**

```bash
cd /Users/d/Code/tesla_automation && .venv/bin/python -c "
import asyncio, base64, json
from config import settings
from tesla import TeslaClient
async def main():
    c = TeslaClient(settings)
    tok = await c._access_token()
    p = tok.split('.')[1]; p += '=' * (-len(p) % 4)
    print('SCOPES:', json.loads(base64.urlsafe_b64decode(p)).get('scp'))
    await c.aclose()
asyncio.run(main())
"
```

Expected: a list containing `vehicle_device_data`, `vehicle_location`, `vehicle_cmds`, `vehicle_charging_cmds`.

If the scopes are missing, the developer-portal change has not propagated (allow ~10 min) — wait and repeat Step 5. Do not proceed without them.

- [ ] **Step 7: Confirm the account's command and billing posture**

```bash
cd /Users/d/Code/tesla_automation && .venv/bin/python -c "
import asyncio, json, httpx
from config import settings
from tesla import TeslaClient
async def main():
    c = TeslaClient(settings)
    tok = await c._access_token()
    async with httpx.AsyncClient(timeout=20) as h:
        r = await h.post(settings.api_base + '/api/1/vehicles/fleet_status',
            headers={'Authorization': f'Bearer {tok}'},
            json={'vins': ['5YJSA1E5XNF477026']})
        print(json.dumps(r.json(), indent=2))
    await c.aclose()
asyncio.run(main())
"
```

Record from the response: `vehicle_command_protocol_required` (expect `true`), `discounted_device_data` (expect `true` — this is the billing answer), `key_paired_vins` (expect empty until Task 10), `fleet_telemetry_version`, `firmware_version`.

- [ ] **Step 8: Commit**

```bash
git add config.py tesla.py
git commit -m "feat: widen OAuth scopes to vehicle data, location, and commands"
```

---

### Task 2: Vehicle read methods on the Fleet API client

**Files:**
- Modify: `tesla.py` (add `VehicleAsleep`, `_post`, and six vehicle methods)
- Modify: `config.py` (add `vin`, `db_file`, proxy and poll settings)
- Create: `tests/test_tesla_paths.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `config.SCOPES` from Task 1.
- Produces:
  - `class VehicleAsleep(RuntimeError)` — raised on HTTP 408.
  - `TeslaClient.vehicles() -> list[dict]`
  - `TeslaClient.vehicle(vin: str) -> dict` — the free state check; has `"state"`.
  - `TeslaClient.fleet_status(vins: list[str]) -> dict`
  - `TeslaClient.vehicle_data(vin: str, endpoints: list[str] | None = None) -> dict`
  - `TeslaClient.wake_up(vin: str) -> dict`
  - `TeslaClient.resolve_vin() -> str`
  - `tesla.VEHICLE_ENDPOINTS: list[str]`
  - `config.Settings.vin`, `.db_file`, `.proxy_url`, `.proxy_cert`, `.poll_driving`, `.poll_charging`, `.poll_idle`, `.poll_asleep`

- [ ] **Step 1: Add pytest**

Append to `requirements.txt`:

```
pytest>=8.0
pytest-asyncio>=0.24
```

Run: `.venv/bin/pip install -r requirements.txt`

- [ ] **Step 2: Write the failing test for endpoint-string construction**

The `endpoints` param must be semicolon-joined. Commas silently return a partial payload, which is the kind of bug that costs an afternoon. Create `tests/test_tesla_paths.py`:

```python
from tesla import VEHICLE_ENDPOINTS, endpoints_param


def test_endpoints_are_semicolon_joined():
    assert endpoints_param(["charge_state", "drive_state"]) == "charge_state;drive_state"


def test_endpoints_default_covers_everything_the_page_needs():
    for group in ("charge_state", "climate_state", "drive_state",
                  "location_data", "vehicle_state", "gui_settings"):
        assert group in VEHICLE_ENDPOINTS


def test_endpoints_param_rejects_commas():
    # A caller passing a pre-joined comma string is the classic mistake.
    try:
        endpoints_param(["charge_state,drive_state"])
    except ValueError:
        return
    raise AssertionError("expected ValueError for a comma-containing group")
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tesla_paths.py -v`
Expected: FAIL with `ImportError: cannot import name 'VEHICLE_ENDPOINTS'`

- [ ] **Step 4: Add the vehicle surface to `tesla.py`**

Add near the top of `tesla.py`, after the existing exception classes:

```python
class VehicleAsleep(RuntimeError):
    """HTTP 408 — the car is asleep or offline. Not an error path; the normal
    resting state. The response has no body at all."""


# Every group the car page reads. `location_data` is what actually yields
# coordinates; the `vehicle_location` scope alone is not enough.
VEHICLE_ENDPOINTS = [
    "charge_state",
    "climate_state",
    "drive_state",
    "location_data",
    "vehicle_state",
    "vehicle_config",
    "gui_settings",
]


def endpoints_param(groups: list[str]) -> str:
    """Tesla wants semicolons. Commas silently return a partial payload."""
    for g in groups:
        if "," in g:
            raise ValueError(f"endpoint group {g!r} contains a comma; pass a list instead")
    return ";".join(groups)
```

Then add these methods to `TeslaClient`, after `power_history`:

```python
    # ---------- vehicles ----------

    async def vehicles(self) -> list[dict[str, Any]]:
        return await self._get("/api/1/vehicles", ttl=60) or []

    async def resolve_vin(self) -> str:
        """Configured VIN wins; otherwise the single vehicle on the account."""
        if self.settings.vin:
            return self.settings.vin
        cars = await self.vehicles()
        if not cars:
            raise TeslaAPIError(404, "No vehicles on this Tesla account.")
        return str(cars[0]["vin"])

    async def vehicle(self, vin: str) -> dict[str, Any]:
        """Cheap state check: online | asleep | offline. Gate paid calls on this."""
        return await self._get(f"/api/1/vehicles/{vin}", ttl=10)

    async def fleet_status(self, vins: list[str]) -> dict[str, Any]:
        return await self._post("/api/1/vehicles/fleet_status", {"vins": vins})

    async def vehicle_data(
        self, vin: str, endpoints: list[str] | None = None
    ) -> dict[str, Any]:
        try:
            return await self._get(
                f"/api/1/vehicles/{vin}/vehicle_data",
                params={"endpoints": endpoints_param(endpoints or VEHICLE_ENDPOINTS)},
                ttl=10,
            )
        except TeslaAPIError as exc:
            if exc.status == 408:
                raise VehicleAsleep(vin) from exc
            raise

    async def wake_up(self, vin: str) -> dict[str, Any]:
        """Returns a vehicle object with `state`, NOT a {result, reason} envelope.
        Expensive and rate-limited to 3/min. Only ever call this on explicit
        user action."""
        return await self._post(f"/api/1/vehicles/{vin}/wake_up", {})
```

Add the `_post` helper next to `_get` (same auth-retry shape):

```python
    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self.settings.api_base}{path}"
        for attempt in range(2):
            token = await self._access_token()
            resp = await self._http.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 401 and attempt == 0:
                current = self.store.load()
                if current is None:
                    raise TeslaAuthError("Not logged in.")
                async with self._refresh_lock:
                    await self._refresh(current)
                continue
            if resp.status_code != 200:
                raise TeslaAPIError(resp.status_code, resp.text)
            return resp.json().get("response")
        raise TeslaAuthError("Could not authenticate to Fleet API.")
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_tesla_paths.py -v`
Expected: 3 passed

- [ ] **Step 6: Add the new settings**

In `config.py`, add these fields to the `Settings` dataclass:

```python
    vin: str = field(default_factory=lambda: _clean(os.getenv("TESLA_VIN")))
    db_file: Path = field(
        default_factory=lambda: Path(_clean(os.getenv("CAR_DB")) or BASE_DIR / "car.db")
    )
    # Signed commands only. Reads go direct so proxy downtime costs us buttons, not data.
    proxy_url: str = field(
        default_factory=lambda: _clean(os.getenv("TESLA_PROXY_URL")) or "https://localhost:4443"
    )
    proxy_cert: Path = field(
        default_factory=lambda: Path(
            _clean(os.getenv("TESLA_PROXY_CERT")) or BASE_DIR / "keys" / "tls-cert.pem"
        )
    )
    # Adaptive poll intervals, seconds. Asleep uses the FREE state check only.
    poll_driving: int = field(default_factory=lambda: int(_clean(os.getenv("POLL_DRIVING")) or 120))
    poll_charging: int = field(default_factory=lambda: int(_clean(os.getenv("POLL_CHARGING")) or 300))
    poll_idle: int = field(default_factory=lambda: int(_clean(os.getenv("POLL_IDLE")) or 900))
    poll_asleep: int = field(default_factory=lambda: int(_clean(os.getenv("POLL_ASLEEP")) or 300))
```

- [ ] **Step 7: Capture a real response as the test fixture**

Real data beats a hand-written fixture, and this is the only chance to get one before writing the normalizer. The car must be awake — check first, and if it is asleep, ask the user to open the Tesla app (which wakes it for free) rather than spending a `wake_up`.

```bash
mkdir -p tests/fixtures
cd /Users/d/Code/tesla_automation && .venv/bin/python -c "
import asyncio, json
from config import settings
from tesla import TeslaClient, VehicleAsleep
async def main():
    c = TeslaClient(settings)
    vin = await c.resolve_vin()
    print('state:', (await c.vehicle(vin)).get('state'))
    try:
        data = await c.vehicle_data(vin)
    except VehicleAsleep:
        print('ASLEEP — open the Tesla app to wake it, then rerun.')
        await c.aclose(); return
    # Redact before it ever touches git.
    data['vin'] = '5YJSA00000F000000'
    for k in ('latitude','longitude','native_latitude','native_longitude',
              'active_route_latitude','active_route_longitude'):
        if k in data.get('drive_state', {}):
            data['drive_state'][k] = 0.0
    if 'active_route_destination' in data.get('drive_state', {}):
        data['drive_state']['active_route_destination'] = 'REDACTED'
    open('tests/fixtures/vehicle_data.json','w').write(json.dumps(data, indent=2))
    print('groups:', sorted(data.keys()))
    await c.aclose()
asyncio.run(main())
"
```

Expected: `groups: ['charge_state', 'climate_state', 'drive_state', 'gui_settings', 'vehicle_config', 'vehicle_state', ...]`

Open `tests/fixtures/vehicle_data.json` and confirm the VIN and all coordinates are redacted before committing.

- [ ] **Step 8: Commit**

```bash
git add tesla.py config.py requirements.txt tests/
git commit -m "feat: add vehicle read methods to the Fleet API client"
```

---

### Task 3: Normalize vehicle data into a view model

`vehicle.py` is the vehicle analogue of `energy.py`: pure functions turning Tesla's sprawling, trap-laden payload into one flat dict the API and UI can trust.

**Files:**
- Create: `vehicle.py`
- Create: `tests/test_vehicle.py`

**Interfaces:**
- Consumes: `tests/fixtures/vehicle_data.json` from Task 2.
- Produces: `vehicle.derive(raw: dict) -> dict` returning the keys asserted in Step 2 below. `store.py`, `car_routes.py`, and `car.js` all consume exactly this shape.

- [ ] **Step 1: Read the field reference first**

Read `docs/tesla-field-reference.md` sections 3.1–3.4 and section 5. The traps this task must encode:

- `is_climate_on` is true for dog mode, COP and preconditioning. Use `is_auto_conditioning_on` for user intent.
- Charging is `charging_state in {"Starting", "Charging"}`. Never infer from voltage — `charger_voltage` reads `2` when idle.
- Doors are driver/passenger-first (`df`=driver **front**, `dr`=driver **rear**); windows are front/rear-first (`fd_window`=**front** driver). `df` and `fd_window` are different corners.
- `0` = closed, non-zero = open, for both.
- `shift_state` and `speed` are `null` when parked. That is normal.
- `"<invalid>"` is a sentinel string, not a value. But `charge_port_color` has a real `"Off"` — do not conflate them.
- `vehicle_state.vehicle_name`, not a top-level `display_name` (which does not exist here).
- `software_update.install_perc` idles at `1`; `version` is often a single space; `status` is `""` when idle.
- `santa_mode` is an int; `native_location_supported` is 0/1.
- Location keys are **absent**, not null, without access.

- [ ] **Step 2: Write the failing test**

Create `tests/test_vehicle.py`:

```python
import json
from pathlib import Path

import pytest

import vehicle

FIXTURE = Path(__file__).parent / "fixtures" / "vehicle_data.json"


@pytest.fixture
def raw():
    return json.loads(FIXTURE.read_text())


def test_derive_produces_the_documented_shape(raw):
    v = vehicle.derive(raw)
    for key in ("vin", "name", "soc", "usable_soc", "limit", "charging",
                "charging_state", "plugged_in", "range_mi", "odometer_mi",
                "locked", "doors", "windows", "sentry", "dashcam",
                "inside_c", "outside_c", "climate_on", "lat", "lon",
                "shift", "speed_mph", "tpms_bar", "software", "sampled_at"):
        assert key in v, f"missing {key}"


def test_charging_is_derived_from_state_not_voltage(raw):
    raw["charge_state"]["charging_state"] = "Disconnected"
    raw["charge_state"]["charger_voltage"] = 2  # the idle sentinel
    assert vehicle.derive(raw)["charging"] is False

    raw["charge_state"]["charging_state"] = "Charging"
    assert vehicle.derive(raw)["charging"] is True

    raw["charge_state"]["charging_state"] = "Starting"
    assert vehicle.derive(raw)["charging"] is True


def test_climate_uses_user_intent_not_any_reason(raw):
    raw["climate_state"]["is_climate_on"] = True
    raw["climate_state"]["is_auto_conditioning_on"] = False
    assert vehicle.derive(raw)["climate_on"] is False


def test_doors_and_windows_do_not_cross_axes(raw):
    raw["vehicle_state"].update(
        {"df": 1, "dr": 0, "pf": 0, "pr": 0,
         "fd_window": 0, "fp_window": 0, "rd_window": 1, "rp_window": 0}
    )
    v = vehicle.derive(raw)
    assert v["doors"] == {"driver_front": True, "driver_rear": False,
                          "passenger_front": False, "passenger_rear": False}
    assert v["windows"] == {"front_driver": False, "front_passenger": False,
                            "rear_driver": True, "rear_passenger": False}


def test_parked_car_reports_no_speed_and_parked_shift(raw):
    raw["drive_state"]["shift_state"] = None
    raw["drive_state"]["speed"] = None
    v = vehicle.derive(raw)
    assert v["shift"] == "P"
    assert v["speed_mph"] == 0


def test_invalid_sentinel_becomes_none_but_off_survives(raw):
    raw["charge_state"]["fast_charger_type"] = "<invalid>"
    raw["charge_state"]["charge_port_color"] = "Off"
    v = vehicle.derive(raw)
    assert v["fast_charger"] is None
    assert v["port_color"] == "Off"


def test_missing_location_keys_are_tolerated(raw):
    for k in ("latitude", "longitude", "heading", "gps_as_of"):
        raw["drive_state"].pop(k, None)
    v = vehicle.derive(raw)
    assert v["lat"] is None and v["lon"] is None


def test_idle_software_update_is_reported_as_none(raw):
    raw["vehicle_state"]["software_update"] = {
        "status": "", "version": " ", "download_perc": 0, "install_perc": 1,
    }
    assert vehicle.derive(raw)["software"] is None


def test_empty_payload_does_not_raise():
    v = vehicle.derive({})
    assert v["soc"] is None
    assert v["doors"] == {}
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_vehicle.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'vehicle'`

- [ ] **Step 4: Write `vehicle.py`**

```python
"""Normalize Tesla `vehicle_data` into one flat, trustworthy view model.

Tesla's payload mixes metric temperatures with imperial distances, uses two
opposite naming conventions for doors and windows, and omits whole keys rather
than nulling them. Every trap encoded here is documented in
docs/tesla-field-reference.md — read that before changing a field access.
"""
from __future__ import annotations

import time
from typing import Any

# Charging is a state machine, not a power reading. `charger_voltage` reads 2
# when idle, so any voltage/power heuristic reports phantom charging.
CHARGING_STATES = {"Starting", "Charging"}

# Tesla's sentinel for "no value" in enum-ish string fields.
INVALID = "<invalid>"

SOFTWARE_ACTIVE = {"available", "downloading", "downloading_wifi_wait",
                   "scheduled", "installing"}


def _s(value: Any) -> str | None:
    """String fields, with Tesla's sentinel mapped to None.

    `charge_port_color` has a genuine "Off" value, so only the explicit
    sentinel and blanks are dropped."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return None if not text or text == INVALID else text


def _open(value: Any) -> bool:
    """Door/window/trunk state. Tesla models these as bools serialized 0/1."""
    return bool(value) and value != 0


def derive(raw: dict[str, Any]) -> dict[str, Any]:
    raw = raw or {}
    charge = raw.get("charge_state") or {}
    climate = raw.get("climate_state") or {}
    drive = raw.get("drive_state") or {}
    state = raw.get("vehicle_state") or {}
    config = raw.get("vehicle_config") or {}

    charging_state = _s(charge.get("charging_state"))

    return {
        "vin": raw.get("vin"),
        # `display_name` does not exist in vehicle_data; the name lives here.
        "name": state.get("vehicle_name") or "Car",
        "state": raw.get("state"),
        "version": (state.get("car_version") or "").split(" ")[0] or None,

        # ---- charge ----
        "soc": charge.get("battery_level"),
        "usable_soc": charge.get("usable_battery_level"),
        "limit": charge.get("charge_limit_soc"),
        "limit_min": charge.get("charge_limit_soc_min"),
        "limit_max": charge.get("charge_limit_soc_max"),
        "charging": charging_state in CHARGING_STATES,
        "charging_state": charging_state,
        "charge_power_kw": charge.get("charger_power"),
        "charge_amps": charge.get("charge_current_request"),
        "amps_max": charge.get("charge_current_request_max"),
        "amps_actual": charge.get("charger_actual_current"),
        "minutes_to_full": charge.get("minutes_to_full_charge")
            if isinstance(charge.get("minutes_to_full_charge"), (int, float)) else None,
        "energy_added_kwh": charge.get("charge_energy_added"),
        "range_mi": charge.get("battery_range"),
        "range_est_mi": charge.get("est_battery_range"),
        "plugged_in": _s(charge.get("conn_charge_cable")) is not None,
        "port_open": bool(charge.get("charge_port_door_open")),
        "port_latch": _s(charge.get("charge_port_latch")),
        "port_color": _s(charge.get("charge_port_color")),
        "fast_charger": _s(charge.get("fast_charger_type")),
        "scheduled_mode": _s(charge.get("scheduled_charging_mode")),

        # ---- climate ----
        "inside_c": climate.get("inside_temp"),
        "outside_c": climate.get("outside_temp"),
        # is_climate_on goes true for dog mode, COP and preconditioning. This
        # is the flag that means "the user asked for climate".
        "climate_on": bool(climate.get("is_auto_conditioning_on")),
        "climate_any": bool(climate.get("is_climate_on")),
        "preconditioning": bool(climate.get("is_preconditioning")),
        "climate_keeper": _s(climate.get("climate_keeper_mode")),
        "defrost": climate.get("defrost_mode"),
        "driver_temp_c": climate.get("driver_temp_setting"),
        "passenger_temp_c": climate.get("passenger_temp_setting"),
        "temp_min_c": climate.get("min_avail_temp"),
        "temp_max_c": climate.get("max_avail_temp"),
        "seat_heaters": {
            name: climate[key]
            for name, key in (
                ("front_left", "seat_heater_left"),
                ("front_right", "seat_heater_right"),
                ("rear_left", "seat_heater_rear_left"),
                ("rear_center", "seat_heater_rear_center"),
                ("rear_right", "seat_heater_rear_right"),
            )
            if key in climate
        },
        "wheel_heater": climate.get("steering_wheel_heater"),
        # Read-only vehicle-side gate. When false, every remote comfort
        # command comes back result:false and there is no command to flip it.
        "comfort_enabled": climate.get("remote_heater_control_enabled", True),

        # ---- position ----
        # These keys are ABSENT without location access, not null.
        "lat": drive.get("latitude"),
        "lon": drive.get("longitude"),
        "heading": drive.get("heading"),
        "speed_mph": drive.get("speed") or 0,
        "shift": _s(drive.get("shift_state")) or "P",
        "power_kw": drive.get("power"),
        "gps_at": drive.get("gps_as_of"),
        "route": _route(drive),

        # ---- body ----
        "odometer_mi": state.get("odometer"),
        "locked": state.get("locked"),
        "doors": {
            name: _open(state[key])
            for name, key in (("driver_front", "df"), ("driver_rear", "dr"),
                              ("passenger_front", "pf"), ("passenger_rear", "pr"))
            if key in state
        },
        "windows": {
            name: _open(state[key])
            for name, key in (("front_driver", "fd_window"),
                              ("front_passenger", "fp_window"),
                              ("rear_driver", "rd_window"),
                              ("rear_passenger", "rp_window"))
            if key in state
        },
        "trunks": {
            name: _open(state[key])
            for name, key in (("front", "ft"), ("rear", "rt")) if key in state
        },
        "sentry": state.get("sentry_mode"),
        "sentry_available": bool(state.get("sentry_mode_available")),
        # No API exposes footage. This is the only camera-adjacent signal there is.
        "dashcam": _s(state.get("dashcam_state")),
        "user_present": bool(state.get("is_user_present")),
        "valet": bool(state.get("valet_mode")),
        "speed_limit": state.get("speed_limit_mode"),
        "tpms_bar": {
            corner: state[f"tpms_pressure_{corner}"]
            for corner in ("fl", "fr", "rl", "rr")
            if f"tpms_pressure_{corner}" in state
        },
        "tpms_warn": {
            corner: bool(state.get(f"tpms_soft_warning_{corner}")
                         or state.get(f"tpms_hard_warning_{corner}"))
            for corner in ("fl", "fr", "rl", "rr")
            if f"tpms_pressure_{corner}" in state
        },
        "software": _software(state.get("software_update")),

        # ---- capability flags, for hiding controls the car cannot do ----
        "has_sunroof": bool(config.get("sun_roof_installed")),
        "has_seat_cooling": bool(config.get("has_seat_cooling")),
        "has_rear_seat_heaters": bool(config.get("rear_seat_heaters")),

        "sampled_at": int(time.time()),
    }


def _route(drive: dict[str, Any]) -> dict[str, Any] | None:
    """Active navigation. These keys vanish individually, not as a group."""
    minutes = drive.get("active_route_minutes_to_arrival")
    if minutes is None:
        return None
    return {
        "destination": drive.get("active_route_destination"),
        "lat": drive.get("active_route_latitude"),
        "lon": drive.get("active_route_longitude"),
        "miles": drive.get("active_route_miles_to_arrival"),
        "minutes": minutes,
        "delay_minutes": drive.get("active_route_traffic_minutes_delay"),
        "soc_at_arrival": drive.get("active_route_energy_at_arrival"),
    }


def _software(update: Any) -> dict[str, Any] | None:
    """Idle sentinels are weird: status "", version " ", install_perc 1.
    Gate on status alone."""
    if not isinstance(update, dict):
        return None
    status = (update.get("status") or "").strip()
    if status not in SOFTWARE_ACTIVE:
        return None
    return {
        "status": status,
        "version": (update.get("version") or "").strip() or None,
        "download_pct": update.get("download_perc"),
        "install_pct": update.get("install_perc"),
        "duration_sec": update.get("expected_duration_sec"),
    }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_vehicle.py -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add vehicle.py tests/test_vehicle.py
git commit -m "feat: normalize vehicle_data into a flat view model"
```

---

### Task 4: SQLite store for state-of-charge history

**Files:**
- Create: `store.py`
- Create: `tests/test_store.py`

**Interfaces:**
- Consumes: `vehicle.derive` output from Task 3.
- Produces:
  - `store.Store(path: Path)` with `.record(view: dict) -> None`, `.snapshot(vin: str) -> dict | None`, `.history(vin, start, end, buckets=240) -> list[dict]`, `.first_sample(vin) -> int | None`, `.count_since(ts) -> int`, `.close()`
  - `.snapshot()` returns `{"view": dict, "ts": int}` or `None`.
  - `.history()` rows are `{"ts": int, "soc": int, "usable_soc": int|None, "charging": bool, "gap": bool}` where `gap=True` means the sample before it is further back than `GAP_SECONDS`.
  - `store.GAP_SECONDS: int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_store.py`:

```python
import time

import pytest

import store


@pytest.fixture
def db(tmp_path):
    s = store.Store(tmp_path / "t.db")
    yield s
    s.close()


def view(ts, soc, charging=False, vin="VIN1"):
    return {"vin": vin, "soc": soc, "usable_soc": soc - 2, "charging": charging,
            "charging_state": "Charging" if charging else "Disconnected",
            "charge_power_kw": 7 if charging else 0, "limit": 80,
            "range_mi": soc * 3.0, "odometer_mi": 1000.0, "inside_c": 21.0,
            "outside_c": 15.0, "lat": 39.7, "lon": -104.9, "shift": "P",
            "sampled_at": ts}


def test_record_then_snapshot_roundtrips(db):
    db.record(view(1000, 55))
    snap = db.snapshot("VIN1")
    assert snap["ts"] == 1000
    assert snap["view"]["soc"] == 55


def test_snapshot_is_none_for_unknown_vin(db):
    assert db.snapshot("NOPE") is None


def test_same_second_write_is_idempotent(db):
    db.record(view(1000, 55))
    db.record(view(1000, 55))
    assert len(db.history("VIN1", 0, 2000)) == 1


def test_history_is_ordered_and_bounded(db):
    for i, soc in enumerate([50, 55, 60, 65]):
        db.record(view(1000 + i * 60, soc))
    rows = db.history("VIN1", 1000, 1120)
    assert [r["soc"] for r in rows] == [50, 55, 60]


def test_gap_flag_marks_sleep_holes(db):
    db.record(view(1000, 80))
    db.record(view(1000 + store.GAP_SECONDS + 1, 78))
    rows = db.history("VIN1", 0, 10 ** 9)
    assert rows[0]["gap"] is False
    assert rows[1]["gap"] is True


def test_adjacent_samples_are_not_gaps(db):
    db.record(view(1000, 80))
    db.record(view(1060, 80))
    rows = db.history("VIN1", 0, 10 ** 9)
    assert [r["gap"] for r in rows] == [False, False]


def test_history_downsamples_to_the_bucket_budget(db):
    for i in range(1000):
        db.record(view(1000 + i * 60, 50))
    rows = db.history("VIN1", 0, 10 ** 9, buckets=100)
    assert 0 < len(rows) <= 100


def test_first_sample_and_count(db):
    db.record(view(1000, 50))
    db.record(view(2000, 51))
    assert db.first_sample("VIN1") == 1000
    assert db.count_since(1500) == 1


def test_two_stores_can_write_concurrently(tmp_path):
    """The launchd collector and the web app both write. WAL must allow it."""
    a = store.Store(tmp_path / "t.db")
    b = store.Store(tmp_path / "t.db")
    a.record(view(1000, 50))
    b.record(view(1060, 51))
    assert len(a.history("VIN1", 0, 10 ** 9)) == 2
    a.close()
    b.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'store'`

- [ ] **Step 3: Write `store.py`**

```python
"""Local SQLite history for state of charge.

The Fleet API has no vehicle history endpoint of any kind, so every point on
the SoC chart is one we recorded ourselves. Two processes write here — the
launchd collector and the web app — so the database runs in WAL mode and every
insert is idempotent on the second.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

# A sleeping car emits nothing. Anything longer than this between samples is a
# hole rather than a slope, and the chart draws it as such.
GAP_SECONDS = 1800

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts                   INTEGER NOT NULL,
  vin                  TEXT    NOT NULL,
  battery_level        INTEGER,
  usable_battery_level INTEGER,
  charge_limit_soc     INTEGER,
  charging_state       TEXT,
  charging             INTEGER,
  charger_power        INTEGER,
  range_mi             REAL,
  odometer             REAL,
  inside_temp          REAL,
  outside_temp         REAL,
  latitude             REAL,
  longitude            REAL,
  shift_state          TEXT,
  PRIMARY KEY (vin, ts)
);
CREATE INDEX IF NOT EXISTS samples_vin_ts ON samples (vin, ts);

CREATE TABLE IF NOT EXISTS snapshot (
  vin  TEXT PRIMARY KEY,
  ts   INTEGER NOT NULL,
  json TEXT    NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._db = sqlite3.connect(self.path, timeout=10.0)
        self._db.row_factory = sqlite3.Row
        # WAL lets the collector and the web app write without blocking.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def record(self, view: dict[str, Any]) -> None:
        """Store one sample plus the full snapshot. Idempotent per (vin, second)
        so a collector poll and a page load landing together cannot double-count."""
        vin = view.get("vin")
        ts = int(view.get("sampled_at") or 0)
        if not vin or not ts or view.get("soc") is None:
            return
        self._db.execute(
            """INSERT OR REPLACE INTO samples
               (ts, vin, battery_level, usable_battery_level, charge_limit_soc,
                charging_state, charging, charger_power, range_mi, odometer,
                inside_temp, outside_temp, latitude, longitude, shift_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, vin, view.get("soc"), view.get("usable_soc"), view.get("limit"),
             view.get("charging_state"), int(bool(view.get("charging"))),
             view.get("charge_power_kw"), view.get("range_mi"),
             view.get("odometer_mi"), view.get("inside_c"), view.get("outside_c"),
             view.get("lat"), view.get("lon"), view.get("shift")),
        )
        self._db.execute(
            "INSERT OR REPLACE INTO snapshot (vin, ts, json) VALUES (?,?,?)",
            (vin, ts, json.dumps(view)),
        )
        self._db.commit()

    def snapshot(self, vin: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT ts, json FROM snapshot WHERE vin = ?", (vin,)
        ).fetchone()
        if row is None:
            return None
        return {"ts": row["ts"], "view": json.loads(row["json"])}

    def first_sample(self, vin: str) -> int | None:
        row = self._db.execute(
            "SELECT MIN(ts) AS t FROM samples WHERE vin = ?", (vin,)
        ).fetchone()
        return row["t"] if row and row["t"] is not None else None

    def count_since(self, ts: int) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM samples WHERE ts >= ?", (ts,)
        ).fetchone()
        return int(row["n"])

    def history(
        self, vin: str, start: int, end: int, buckets: int = 240
    ) -> list[dict[str, Any]]:
        """Samples in [start, end), downsampled to at most `buckets` points.

        Bucketing takes the last sample in each bucket rather than an average,
        so a charge that finishes mid-bucket still reads as finished."""
        span = max(1, end - start)
        width = max(1, span // max(1, buckets))
        rows = self._db.execute(
            """SELECT ts, battery_level, usable_battery_level, charging
               FROM samples
               WHERE vin = ? AND ts >= ? AND ts < ?
                 AND ts IN (
                   SELECT MAX(ts) FROM samples
                   WHERE vin = ? AND ts >= ? AND ts < ?
                   GROUP BY (ts - ?) / ?
                 )
               ORDER BY ts""",
            (vin, start, end, vin, start, end, start, width),
        ).fetchall()

        out: list[dict[str, Any]] = []
        previous: int | None = None
        for row in rows:
            out.append({
                "ts": row["ts"],
                "soc": row["battery_level"],
                "usable_soc": row["usable_battery_level"],
                "charging": bool(row["charging"]),
                # True where the car was asleep and we have no samples. The
                # chart draws these segments dashed rather than inventing a slope.
                "gap": previous is not None and row["ts"] - previous > GAP_SECONDS,
            })
            previous = row["ts"]
        return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_store.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add store.py tests/test_store.py
git commit -m "feat: add SQLite store for state-of-charge history"
```

---

### Task 5: Adaptive collector under launchd

This is the task that makes the SoC chart possible, so it ships before the UI that renders it. History accrues from the moment this loads.

**Files:**
- Create: `collector.py`
- Create: `tests/test_collector.py`
- Create: `com.tenxcious.tesla-collector.plist`

**Interfaces:**
- Consumes: `tesla.TeslaClient`, `vehicle.derive`, `store.Store`.
- Produces:
  - `collector.next_interval(car_state: str, view: dict | None, settings) -> int` — pure, tested.
  - `collector.poll_once(client, store, vin, settings) -> tuple[str, dict | None]` returning `(car_state, view_or_None)`.
  - `python collector.py` runs the loop; `python collector.py --once` polls once and exits.

- [ ] **Step 1: Write the failing test for the pure interval logic**

Create `tests/test_collector.py`:

```python
from types import SimpleNamespace

import collector

S = SimpleNamespace(poll_driving=120, poll_charging=300, poll_idle=900, poll_asleep=300)


def test_asleep_uses_the_free_check_interval():
    assert collector.next_interval("asleep", None, S) == 300
    assert collector.next_interval("offline", None, S) == 300


def test_driving_polls_fastest():
    assert collector.next_interval("online", {"shift": "D", "charging": False}, S) == 120
    assert collector.next_interval("online", {"shift": "R", "charging": False}, S) == 120


def test_charging_polls_at_the_charging_interval():
    assert collector.next_interval("online", {"shift": "P", "charging": True}, S) == 300


def test_driving_wins_over_charging():
    # Both flags can be set momentarily; the faster cadence must win.
    assert collector.next_interval("online", {"shift": "D", "charging": True}, S) == 120


def test_online_and_idle_polls_slowest():
    assert collector.next_interval("online", {"shift": "P", "charging": False}, S) == 900


def test_online_but_no_data_falls_back_to_idle():
    assert collector.next_interval("online", None, S) == 900
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_collector.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'collector'`

- [ ] **Step 3: Write `collector.py`**

```python
"""Adaptive state-of-charge collector.

Runs independently of the web app under launchd so history stays continuous
when the dashboard is closed.

Two rules make this cheap and safe:
  * Every paid vehicle_data call is gated on the FREE state check, because a
    408 from a sleeping car is billed like any other request.
  * It never wakes the car. On 2021+ vehicles polling does not prevent sleep —
    only commands do — so this costs no vampire drain.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

import vehicle
from config import settings
from store import Store
from tesla import TeslaAPIError, TeslaAuthError, TeslaClient, VehicleAsleep

DRIVING = {"D", "R", "N"}


def next_interval(car_state: str, view: dict | None, cfg) -> int:
    """Seconds until the next poll. Pure, so it is testable without a car."""
    if car_state != "online":
        return cfg.poll_asleep
    if view is None:
        return cfg.poll_idle
    # Driving changes SoC fastest, and a drive is short — check it first so a
    # car that is both moving and (briefly) charging still samples densely.
    if view.get("shift") in DRIVING:
        return cfg.poll_driving
    if view.get("charging"):
        return cfg.poll_charging
    return cfg.poll_idle


async def poll_once(client: TeslaClient, store: Store, vin: str, cfg):
    """One cycle: free state check, then a paid read only if it can succeed."""
    try:
        car_state = (await client.vehicle(vin)).get("state") or "offline"
    except TeslaAPIError as exc:
        _log(f"state check failed: {exc}")
        return "offline", None

    if car_state != "online":
        return car_state, None

    try:
        view = vehicle.derive(await client.vehicle_data(vin))
    except VehicleAsleep:
        # Documented: vehicle_data can 408 even when /vehicles says online.
        return "asleep", None
    except TeslaAPIError as exc:
        _log(f"vehicle_data failed: {exc}")
        return car_state, None

    store.record(view)
    return car_state, view


async def run(once: bool = False) -> int:
    client = TeslaClient(settings)
    store = Store(settings.db_file)
    try:
        vin = await client.resolve_vin()
    except (TeslaAPIError, TeslaAuthError) as exc:
        _log(f"cannot resolve VIN: {exc}")
        await client.aclose()
        store.close()
        return 1

    _log(f"collecting for {vin}")
    try:
        while True:
            try:
                car_state, view = await poll_once(client, store, vin, settings)
            except TeslaAuthError as exc:
                # Nothing to retry against; launchd will restart us later.
                _log(f"auth lost: {exc}")
                return 1
            soc = (view or {}).get("soc")
            _log(f"{car_state}" + (f" soc={soc}%" if soc is not None else ""))
            if once:
                return 0
            await asyncio.sleep(next_interval(car_state, view, settings))
    finally:
        await client.aclose()
        store.close()


def _log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    sys.exit(asyncio.run(run(once=parser.parse_args().once)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_collector.py -v`
Expected: 6 passed

- [ ] **Step 5: Do a single real poll**

```bash
cd /Users/d/Code/tesla_automation && .venv/bin/python collector.py --once
```

Expected: a line like `2026-07-25T19:20:01 online soc=64%`, or `... offline` if the car is asleep — both are correct outcomes. If it printed a SoC, confirm it landed:

```bash
.venv/bin/sqlite3 car.db "SELECT ts, vin, battery_level, charging_state FROM samples;" 2>/dev/null \
  || .venv/bin/python -c "
import sqlite3; d=sqlite3.connect('car.db')
print(d.execute('SELECT ts, battery_level, charging_state FROM samples').fetchall())"
```

- [ ] **Step 6: Write the launchd agent**

Create `com.tenxcious.tesla-collector.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.tenxcious.tesla-collector</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/d/Code/tesla_automation/.venv/bin/python</string>
    <string>/Users/d/Code/tesla_automation/collector.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/d/Code/tesla_automation</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>/Users/d/Code/tesla_automation/collector.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/d/Code/tesla_automation/collector.log</string>
</dict>
</plist>
```

Add `collector.log` to `.gitignore`.

- [ ] **Step 7: Install and verify the agent is running**

```bash
cp /Users/d/Code/tesla_automation/com.tenxcious.tesla-collector.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.tenxcious.tesla-collector.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.tenxcious.tesla-collector.plist
sleep 5
launchctl list | grep tesla-collector
tail -5 /Users/d/Code/tesla_automation/collector.log
```

Expected: `launchctl list` shows the label with exit status `0` in the second column, and the log shows a `collecting for 5YJSA...` line followed by a state line.

- [ ] **Step 8: Commit**

```bash
git add collector.py tests/test_collector.py com.tenxcious.tesla-collector.plist .gitignore
git commit -m "feat: adaptive SoC collector running under launchd"
```

---

### Task 6: Extract shared frontend modules

Behavior-preserving refactor. The car page needs ~400 lines of chart machinery that currently lives inside the solar page; duplicating it would be worse than extracting it. The solar page is the regression test.

**Files:**
- Create: `static/chart.js`, `static/shared.js`
- Modify: `static/app.js`, `static/index.html:152` (the script tag)

**Interfaces:**
- Consumes: nothing.
- Produces, from `static/chart.js`:
  `SVG_NS`, `PAD`, `HEIGHT`, `el(tag, attrs)`, `barPath(x,y,w,h,r,roundTop)`, `niceTicks(min,max,count)`, `chartFrame(host,{yLo,yHi,yTicks,unit,width})`, `xTickEvery(n,plotW)`, `showTip(evt,title,rows)`, `hideTip()`, `attachCrosshair(svg,rows,xScale,plotW,plotH,rowsFor,titleFor)`, `renderLegend(host,items)`
  From `static/shared.js`:
  `$(id)`, `api(path,opts)`, `COLOR(name)`, `nfmt(v,d)`, `initTheme(onChange)`, `showGate(config,message)`

- [ ] **Step 1: Establish the regression baseline**

```bash
lsof -ti:8000 | xargs kill -9 2>/dev/null
cd /Users/d/Code/tesla_automation && DEMO=1 .venv/bin/python app.py > /tmp/demo.log 2>&1 &
sleep 2 && curl -s "localhost:8000/api/dashboard?period=today" | head -c 200
```

Open http://localhost:8000 and confirm before touching anything: charts render, tooltips follow the pointer, the period tabs switch, the table toggle works, and dark mode flips. This is what must still be true at Step 6.

- [ ] **Step 2: Create `static/chart.js`**

Move these from `static/app.js` verbatim, adding `export` to each: `SVG_NS` (line 5), `el` (60-66), `barPath` (69-75), `niceTicks` (78-89), `PAD` and `HEIGHT` (91-92), `chartFrame` (94-123), `xTickEvery` (125-129), `showTip` (135-168), `hideTip` (170), `attachCrosshair` (411-459), `renderLegend` (463-476).

Two changes while moving:

```javascript
// chartFrame renders tick labels, so it needs the formatter.
import { $, nfmt } from "./shared.js";

// The tooltip element is resolved per call rather than at module load, so the
// module can be imported before the DOM exists.
export function showTip(evt, title, rows) {
  const tip = $("tooltip");
  ...
}

export const hideTip = () => { const t = $("tooltip"); if (t) t.hidden = true; };
```

Everywhere `tip` was referenced inside `showTip`, use the locally-resolved `tip`.

- [ ] **Step 3: Create `static/shared.js`**

```javascript
/* Shared across the solar and car pages. */

export const $ = (id) => document.getElementById(id);

/* Entity -> color. One entity keeps its hue across every chart. Read from CSS
   so the light/dark steps stay defined in exactly one place. */
export const COLOR = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim();

export const nfmt = (v, d = 1) =>
  (v ?? 0).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });

export async function api(path, opts) {
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    const err = new Error(body.detail || `Request failed (${resp.status})`);
    err.status = resp.status;
    err.kind = body.error;
    throw err;
  }
  return resp.json();
}

/* Charts read their hues from CSS, so a theme flip has to re-render them. */
export function initTheme(onChange) {
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.dataset.theme = saved;
  const btn = $("theme-toggle");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const current =
      document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("theme", next);
    onChange?.();
  });
}
```

Then move `showGate` (app.js 706-742) into `shared.js`, changing its signature from reading the module-level `state` to taking config explicitly:

```javascript
export function showGate(config, message) {
  // body identical to app.js:706-742, with `const cfg = state.config;`
  // replaced by `const cfg = config;`
}
```

- [ ] **Step 4: Rewire `static/app.js`**

Add at the top of `app.js`, replacing the definitions that moved:

```javascript
import {
  PAD, HEIGHT, el, barPath, niceTicks, chartFrame, xTickEvery,
  showTip, hideTip, attachCrosshair, renderLegend,
} from "./chart.js";
import { $, api, COLOR, nfmt, initTheme, showGate as gate } from "./shared.js";
```

Delete the moved definitions from `app.js`. Keep `kwh`, `kw`, `money`, `bucketLabel`, `fullLabel`, `timeLabel`, `renderLines`, `renderDivergingBars`, `renderDivergingArea`, `renderStackedBars`, `renderTable`, `renderLive`, `render`, `load`, `initPeriods`, `main` — these are solar-specific.

Replace the local `showGate` and `initTheme` with wrappers that supply the module's state:

```javascript
const showGate = (message) => {
  $("app").hidden = true;
  $("gate").hidden = false;
  gate(state.config, message);
};
```

Adjust `initTheme()` in `main()` to `initTheme(() => render())`.

- [ ] **Step 5: Switch the page to ES modules**

In `static/index.html`, replace the script tag:

```html
<script type="module" src="/app.js"></script>
```

- [ ] **Step 6: Verify the solar page is byte-for-byte unchanged in behavior**

Reload http://localhost:8000 with the console open. Confirm:
- No console errors (a module resolution failure shows here first).
- All four charts render.
- Tooltip and crosshair follow the pointer; arrow keys move the crosshair when the plot is focused.
- Period tabs switch and re-render.
- Table toggle shows the table.
- Dark mode flips and charts re-render with the dark steps.

If anything regressed, fix it before committing. This refactor has no user-visible deliverable of its own — its only job is to change nothing.

- [ ] **Step 7: Commit**

```bash
git add static/chart.js static/shared.js static/app.js static/index.html
git commit -m "refactor: extract chart and shared modules from the solar page"
```

---

### Task 7: Car API routes and demo data

**Files:**
- Create: `car_routes.py`
- Modify: `app.py` (mount the router, before the StaticFiles mount)
- Modify: `demo.py` (synthetic vehicle data and history)
- Create: `tests/test_car_routes.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces these endpoints, all under `/api/car`:
  - `GET /state` → `{"vin", "view": dict|null, "age_seconds": int|null, "car_state": str, "live": bool, "source": "live"|"snapshot"|"none"}`
  - `GET /history?range=24h|7d|30d|90d|all` → `{"rows": [...], "since": int|null, "range": str}`
  - `POST /wake` → `{"state": str}`
  - `GET /health` → `{"scopes": [...], "missing_scopes": [...], "key_paired": bool|null, "proxy": bool, "collector": {"running": bool, "last_sample": int|null}, "calls_today": int}`
- Also produces `car_routes.RANGES: dict[str, int]` mapping range keys to seconds (`"all"` maps to `0`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_car_routes.py`:

```python
from fastapi.testclient import TestClient

import app as app_module


def test_ranges_cover_the_ui_options():
    from car_routes import RANGES
    assert set(RANGES) == {"24h", "7d", "30d", "90d", "all"}
    assert RANGES["24h"] == 86400
    assert RANGES["7d"] == 7 * 86400
    assert RANGES["all"] == 0


def test_demo_state_is_live_and_shaped():
    client = TestClient(app_module.app)
    body = client.get("/api/car/state").json()
    assert body["car_state"] == "online"
    assert body["source"] == "live"
    assert body["view"]["soc"] is not None
    assert body["view"]["name"]


def test_demo_history_has_rows_and_a_gap():
    client = TestClient(app_module.app)
    body = client.get("/api/car/history?range=7d").json()
    assert len(body["rows"]) > 10
    assert any(r["gap"] for r in body["rows"]), "demo history must exercise sleep gaps"
    assert all("soc" in r and "ts" in r for r in body["rows"])


def test_history_rejects_an_unknown_range():
    client = TestClient(app_module.app)
    assert client.get("/api/car/history?range=nope").status_code == 400
```

Note: these run against demo mode. Add to `tests/conftest.py`:

```python
import os

os.environ["DEMO"] = "1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_car_routes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'car_routes'`

- [ ] **Step 3: Add synthetic vehicle data to `demo.py`**

Append to `demo.py`:

```python
# ---------------------------------------------------------------- vehicle

def vehicle_data(tz: str) -> dict:
    """Synthetic vehicle_data shaped exactly like Tesla's, so it flows through
    the real vehicle.derive path. The car page cannot be built against a real
    car that is asleep most of the time."""
    now = datetime.now(ZoneInfo(tz))
    charging = 1 <= now.hour < 5
    soc = 62 + (now.hour % 7)
    return {
        "vin": "5YJSA00000F000000",
        "state": "online",
        "charge_state": {
            "battery_level": soc,
            "usable_battery_level": soc - 2,
            "charge_limit_soc": 80,
            "charge_limit_soc_min": 50,
            "charge_limit_soc_max": 100,
            "charging_state": "Charging" if charging else "Disconnected",
            "charger_power": 11 if charging else 0,
            "charger_voltage": 240 if charging else 2,
            "charger_actual_current": 48 if charging else 0,
            "charge_current_request": 48,
            "charge_current_request_max": 48,
            "minutes_to_full_charge": 95 if charging else 0,
            "charge_energy_added": 14.2 if charging else 0.0,
            "battery_range": soc * 3.4,
            "est_battery_range": soc * 3.1,
            "conn_charge_cable": "IEC" if charging else "<invalid>",
            "charge_port_door_open": charging,
            "charge_port_latch": "Engaged" if charging else "Disengaged",
            "charge_port_color": "FlashingGreen" if charging else "Off",
            "fast_charger_type": "<invalid>",
            "scheduled_charging_mode": "Off",
        },
        "climate_state": {
            "inside_temp": 21.5, "outside_temp": 14.0,
            "driver_temp_setting": 21.0, "passenger_temp_setting": 21.0,
            "min_avail_temp": 15.0, "max_avail_temp": 28.0,
            "is_climate_on": False, "is_auto_conditioning_on": False,
            "is_preconditioning": False, "climate_keeper_mode": "off",
            "defrost_mode": 0,
            "seat_heater_left": 0, "seat_heater_right": 0,
            "steering_wheel_heater": False,
            "remote_heater_control_enabled": True,
        },
        "drive_state": {
            # Denver, so the map has somewhere to point.
            "latitude": 39.7392, "longitude": -104.9903, "heading": 215,
            "speed": None, "shift_state": None, "power": 0,
            "gps_as_of": int(now.timestamp()),
            "timestamp": int(now.timestamp() * 1000),
        },
        "vehicle_state": {
            "vehicle_name": "Stallion (demo)",
            "odometer": 24680.5,
            "locked": True,
            "df": 0, "dr": 0, "pf": 0, "pr": 0, "ft": 0, "rt": 0,
            "fd_window": 0, "fp_window": 0, "rd_window": 0, "rp_window": 0,
            "sentry_mode": True, "sentry_mode_available": True,
            "dashcam_state": "Recording",
            "is_user_present": False, "valet_mode": False,
            "car_version": "2026.14.3 abcdef0",
            "tpms_pressure_fl": 3.1, "tpms_pressure_fr": 3.1,
            "tpms_pressure_rl": 3.0, "tpms_pressure_rr": 2.6,
            "tpms_soft_warning_rr": True,
            "software_update": {"status": "", "version": " ",
                                "download_perc": 0, "install_perc": 1},
        },
        "vehicle_config": {"rear_seat_heaters": 1, "has_seat_cooling": False,
                           "sun_roof_installed": 0},
        "gui_settings": {"gui_distance_units": "mi/hr"},
    }


def soc_history(days: int, tz: str) -> list[dict]:
    """A week of plausible SoC: overnight charges, daily drives, and — the point
    of this fixture — sleep gaps the chart has to render as dashed."""
    zone = ZoneInfo(tz)
    now = datetime.now(zone)
    rows: list[dict] = []
    soc = 70
    start = now - timedelta(days=days)
    step = 300
    t = start
    while t < now:
        hour = t.hour
        asleep = 5 <= hour < 7 or 10 <= hour < 15
        if not asleep:
            if 1 <= hour < 5 and soc < 80:
                soc = min(80, soc + 1)
                charging = True
            else:
                charging = False
                if 7 <= hour < 9 or 17 <= hour < 19:
                    soc = max(12, soc - 1)
            rows.append({
                "ts": int(t.timestamp()),
                "soc": soc,
                "usable_soc": soc - 2,
                "charging": charging,
                "gap": False,
            })
        t += timedelta(seconds=step)

    # Mark the first sample after each hole, exactly as store.history does.
    for i in range(1, len(rows)):
        rows[i]["gap"] = rows[i]["ts"] - rows[i - 1]["ts"] > 1800
    return rows
```

Add `timedelta` to the `datetime` import in `demo.py` if it is not already there.

- [ ] **Step 4: Write `car_routes.py`**

```python
"""HTTP surface for the car page.

Reads go direct to the Fleet API; commands (Task 11) go through the signing
proxy. Everything here tolerates a sleeping car, because that is its normal
state — a request that cannot reach the vehicle falls back to the last stored
snapshot with its age, rather than failing."""
from __future__ import annotations

import os
import socket
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query

import demo
import vehicle
from config import settings
from store import Store
from tesla import TeslaAPIError, TeslaClient, VehicleAsleep

DEMO = os.getenv("DEMO", "").strip() in {"1", "true", "yes"}

RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400,
          "90d": 90 * 86400, "all": 0}

REQUIRED_SCOPES = ["vehicle_device_data", "vehicle_location",
                   "vehicle_cmds", "vehicle_charging_cmds"]

router = APIRouter(prefix="/api/car")

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(settings.db_file)
    return _store


def _client() -> TeslaClient:
    from app import client
    return client


async def _vin() -> str:
    return "5YJSA00000F000000" if DEMO else await _client().resolve_vin()


@router.get("/state")
async def car_state() -> dict[str, Any]:
    """Live data when the car is awake, the stored snapshot with its age when not."""
    vin = await _vin()

    if DEMO:
        view = vehicle.derive(demo.vehicle_data(settings.timezone))
        return {"vin": vin, "view": view, "age_seconds": 0,
                "car_state": "online", "live": True, "source": "live"}

    client = _client()
    try:
        car = (await client.vehicle(vin)).get("state") or "offline"
    except TeslaAPIError:
        car = "offline"

    if car == "online":
        try:
            view = vehicle.derive(await client.vehicle_data(vin))
            store().record(view)  # a page view contributes history for free
            return {"vin": vin, "view": view, "age_seconds": 0,
                    "car_state": car, "live": True, "source": "live"}
        except (VehicleAsleep, TeslaAPIError):
            car = "asleep"

    snap = store().snapshot(vin)
    if snap is None:
        return {"vin": vin, "view": None, "age_seconds": None,
                "car_state": car, "live": False, "source": "none"}
    return {"vin": vin, "view": snap["view"],
            "age_seconds": max(0, int(time.time()) - snap["ts"]),
            "car_state": car, "live": False, "source": "snapshot"}


@router.get("/history")
async def car_history(range: str = Query("24h")) -> dict[str, Any]:
    if range not in RANGES:
        raise HTTPException(400, f"range must be one of {sorted(RANGES)}")
    vin = await _vin()

    if DEMO:
        days = {"24h": 1, "7d": 7, "30d": 30, "90d": 90, "all": 90}[range]
        rows = demo.soc_history(days, settings.timezone)
        return {"rows": rows, "since": rows[0]["ts"] if rows else None,
                "range": range}

    now = int(time.time())
    first = store().first_sample(vin)
    if range == "all":
        # Bucket across the real data span, not an arbitrary window, or a sparse
        # history collapses into a single point.
        start = first if first is not None else now
    else:
        start = now - RANGES[range]
    rows = store().history(vin, start, now + 1)
    return {"rows": rows, "since": first, "range": range}


@router.post("/wake")
async def car_wake() -> dict[str, Any]:
    """Explicit user action only. Never called on a timer or a page load."""
    if DEMO:
        return {"state": "online"}
    vin = await _vin()
    result = await _client().wake_up(vin)
    return {"state": (result or {}).get("state", "unknown")}


@router.get("/health")
async def car_health() -> dict[str, Any]:
    vin = await _vin()
    scopes: list[str] = []
    key_paired: bool | None = None

    if not DEMO:
        import base64
        import json
        try:
            token = await _client()._access_token()
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            scopes = json.loads(base64.urlsafe_b64decode(payload)).get("scp", [])
        except Exception:
            scopes = []
        try:
            status = await _client().fleet_status([vin])
            key_paired = vin in (status or {}).get("key_paired_vins", [])
        except TeslaAPIError:
            key_paired = None

    last = store().snapshot(vin)
    day_ago = int(time.time()) - 86400
    return {
        "scopes": scopes,
        "missing_scopes": [s for s in REQUIRED_SCOPES if scopes and s not in scopes],
        "key_paired": True if DEMO else key_paired,
        "proxy": True if DEMO else _proxy_up(),
        "collector": {
            "running": bool(last and int(time.time()) - last["ts"] < 3600),
            "last_sample": last["ts"] if last else None,
        },
        "calls_today": store().count_since(day_ago),
    }


def _proxy_up() -> bool:
    """TCP reachability only — cheap, and enough to tell the UI whether to
    enable the controls."""
    parsed = urlparse(settings.proxy_url)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 443), timeout=0.5
        ):
            return True
    except OSError:
        return False
```

- [ ] **Step 5: Mount the router in `app.py`**

In `app.py`, after the `api_dashboard` function and **before** `app.mount("/", StaticFiles(...))` — the static mount is a catch-all and swallows anything registered after it:

```python
import car_routes

app.include_router(car_routes.router)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_car_routes.py -v`
Expected: 4 passed

- [ ] **Step 7: Verify against the running demo server**

```bash
lsof -ti:8000 | xargs kill -9 2>/dev/null
cd /Users/d/Code/tesla_automation && DEMO=1 .venv/bin/python app.py > /tmp/demo.log 2>&1 &
sleep 2
curl -s localhost:8000/api/car/state | head -c 400; echo
curl -s "localhost:8000/api/car/history?range=7d" | head -c 200; echo
curl -s localhost:8000/api/car/health
```

- [ ] **Step 8: Commit**

```bash
git add car_routes.py app.py demo.py tests/
git commit -m "feat: car API routes with demo data"
```

---

### Task 8: Car page shell, hero, tiles, and map

**Files:**
- Create: `static/car.html`, `static/car.js`
- Create: `static/vendor/leaflet.js`, `static/vendor/leaflet.css`, `static/vendor/images/*`
- Modify: `static/styles.css` (car page styles, nav)
- Modify: `static/index.html` (nav link to the car page)

**Interfaces:**
- Consumes: `/api/car/state`, `/api/car/health`, `/api/car/wake`; `shared.js` and `chart.js` from Task 6.
- Produces: `car.js` exports nothing; it owns `#car-app`. `renderState(body)` is its single render entry point, called on load and on every refresh.

- [ ] **Step 1: Vendor Leaflet**

No CDN at runtime — the rest of this app has no external script dependencies and this should not be the exception.

```bash
cd /Users/d/Code/tesla_automation
mkdir -p static/vendor/images
curl -sL https://unpkg.com/leaflet@1.9.4/dist/leaflet.js  -o static/vendor/leaflet.js
curl -sL https://unpkg.com/leaflet@1.9.4/dist/leaflet.css -o static/vendor/leaflet.css
for f in marker-icon.png marker-icon-2x.png marker-shadow.png layers.png layers-2x.png; do
  curl -sL "https://unpkg.com/leaflet@1.9.4/dist/images/$f" -o "static/vendor/images/$f"
done
ls -la static/vendor static/vendor/images
```

Expected: `leaflet.js` ≈ 147 KB, `leaflet.css` ≈ 15 KB, five images present.

- [ ] **Step 2: Add the nav to the solar page**

In `static/index.html`, inside `.topbar-actions`, before the theme toggle:

```html
<a class="ghost-btn" href="/car.html">Car →</a>
```

- [ ] **Step 3: Write `static/car.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Car · Charge & Controls</title>
<link rel="stylesheet" href="/vendor/leaflet.css">
<link rel="stylesheet" href="/styles.css">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='14' font-size='14'>🚗</text></svg>">
</head>
<body>

<section id="gate" class="gate" hidden>
  <div class="gate-card">
    <h1 id="gate-title">Connect your Tesla account</h1>
    <p id="gate-body" class="muted"></p>
    <div id="gate-actions" class="gate-actions"></div>
    <p id="gate-error" class="gate-error" hidden></p>
  </div>
</section>

<main id="car-app" hidden>

  <header class="topbar">
    <div class="titles">
      <h1 id="car-name">Car</h1>
      <p id="car-status" class="muted small"></p>
    </div>
    <div class="topbar-actions">
      <a class="ghost-btn" href="/index.html">← Solar</a>
      <button id="wake" class="ghost-btn" type="button" hidden>Wake car</button>
      <button id="theme-toggle" class="ghost-btn" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">◐</button>
    </div>
  </header>

  <!-- Staleness is a first-class state, not an error. -->
  <div id="stale-bar" class="stale-bar" hidden></div>
  <div id="error-bar" class="error-bar" hidden></div>

  <section class="hero" aria-label="Charge">
    <div class="hero-ring">
      <svg viewBox="0 0 120 120" role="img" aria-labelledby="hero-soc-label">
        <circle class="ring-track" cx="60" cy="60" r="52"></circle>
        <circle id="ring-fill" class="ring-fill" cx="60" cy="60" r="52"></circle>
        <circle id="ring-limit" class="ring-limit" cx="60" cy="60" r="52"></circle>
      </svg>
      <div class="hero-ring-label">
        <span id="hero-soc">—</span><span class="unit">%</span>
      </div>
    </div>
    <div class="hero-facts">
      <p id="hero-soc-label" class="hero-headline">—</p>
      <p id="hero-sub" class="muted"></p>
      <dl class="hero-dl">
        <div><dt>Range</dt><dd id="hero-range">—</dd></div>
        <div><dt>Limit</dt><dd id="hero-limit">—</dd></div>
        <div><dt>Odometer</dt><dd id="hero-odo">—</dd></div>
      </dl>
    </div>
  </section>

  <section id="tiles" class="live" aria-label="Vehicle status"></section>

  <section class="card">
    <div class="card-head"><h2>Location</h2><span id="map-note" class="muted small"></span></div>
    <div id="map" class="map"></div>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>State of charge</h2>
      <div id="soc-range" class="segmented" role="tablist" aria-label="Range"></div>
    </div>
    <div id="soc-chart" class="chart"></div>
    <p id="soc-note" class="muted small"></p>
  </section>

  <section id="controls" class="card" aria-label="Controls"></section>

  <footer class="foot">
    <span id="foot-collector" class="muted small"></span>
    <span id="foot-calls" class="muted small"></span>
    <span id="foot-version" class="muted small"></span>
  </footer>

</main>

<div id="tooltip" class="tooltip" role="tooltip" hidden></div>
<div id="toast" class="toast" role="status" hidden></div>

<script src="/vendor/leaflet.js"></script>
<script type="module" src="/car.js"></script>
</body>
</html>
```

- [ ] **Step 4: Write the shell of `static/car.js`**

```javascript
/* Car page. Reuses the solar page's chart primitives; adds a Leaflet map.

   The car is asleep most of the time, so every render runs against one of four
   states: live, snapshot (with age), needs-setup, or empty. Which one is in
   force is always visible — a stale number presented as live is worse than no
   number. */

import { $, api, COLOR, nfmt, initTheme, showGate as gate } from "./shared.js";
import { PAD, HEIGHT, el, niceTicks, chartFrame, showTip, hideTip, attachCrosshair }
  from "./chart.js";

const state = {
  config: null,
  body: null,        // last /api/car/state response
  health: null,
  range: "24h",
  history: null,
  map: null,
  marker: null,
};

const RANGES = [
  { key: "24h", label: "24h" },
  { key: "7d", label: "7 days" },
  { key: "30d", label: "30 days" },
  { key: "90d", label: "90 days" },
  { key: "all", label: "All" },
];

/* Wire units, converted only at render time. gui_settings never changes the payload. */
const miles = (v) => (v == null ? "—" : `${nfmt(v, 0)} mi`);
const celsius = (v) => (v == null ? "—" : `${nfmt(v, 0)}°C`);
const psi = (bar) => (bar == null ? "—" : `${nfmt(bar * 14.5038, 0)} psi`);

function ago(seconds) {
  if (seconds == null) return "";
  if (seconds < 90) return "just now";
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
}

function toast(message, ok = true) {
  const box = $("toast");
  box.textContent = message;
  box.classList.toggle("bad", !ok);
  box.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { box.hidden = true; }, 4000);
}
```

- [ ] **Step 5: Add hero, tiles, and staleness rendering to `car.js`**

```javascript
function renderState(body) {
  state.body = body;
  const v = body.view;

  $("car-name").textContent = v?.name || "Car";
  $("wake").hidden = body.car_state === "online";

  // Staleness is surfaced, never hidden. A snapshot presented as live is a lie.
  const stale = $("stale-bar");
  if (body.source === "snapshot") {
    stale.hidden = false;
    stale.textContent =
      `Car is ${body.car_state}. Showing the last reading from ${ago(body.age_seconds)}.`;
  } else if (body.source === "none") {
    stale.hidden = false;
    stale.textContent =
      `Car is ${body.car_state} and we have no stored reading yet. Wake it to see live data.`;
  } else {
    stale.hidden = true;
  }
  $("car-status").textContent =
    body.source === "live" ? "Live" : body.source === "snapshot" ? ago(body.age_seconds) : "";

  if (!v) return;

  // Hero ring. r=52 -> circumference 326.7.
  const C = 2 * Math.PI * 52;
  const soc = v.soc ?? 0;
  const fill = $("ring-fill");
  fill.style.strokeDasharray = `${(soc / 100) * C} ${C}`;
  fill.classList.toggle("charging", !!v.charging);

  // The limit is a notch on the ring, so "how much further will it charge" is
  // readable at a glance rather than by comparing two numbers.
  const limit = $("ring-limit");
  if (v.limit == null) {
    limit.style.display = "none";
  } else {
    limit.style.display = "";
    limit.style.strokeDasharray = `1.5 ${C}`;
    limit.style.strokeDashoffset = `${-(v.limit / 100) * C}`;
  }

  $("hero-soc").textContent = v.soc ?? "—";
  $("hero-soc-label").textContent = headline(v);
  $("hero-sub").textContent = subhead(v);
  $("hero-range").textContent = miles(v.range_mi);
  $("hero-limit").textContent = v.limit == null ? "—" : `${v.limit}%`;
  $("hero-odo").textContent = miles(v.odometer_mi);

  renderTiles(v);
  renderMap(v);
  renderControlsAvailability();
}

function headline(v) {
  if (v.charging) return `Charging at ${v.charge_power_kw ?? "—"} kW`;
  if (v.charging_state === "Complete") return "Charge complete";
  if (v.shift && v.shift !== "P") return `Driving · ${v.speed_mph ?? 0} mph`;
  if (v.plugged_in) return "Plugged in, not charging";
  return "Parked";
}

function subhead(v) {
  if (v.charging && v.minutes_to_full) {
    const h = Math.floor(v.minutes_to_full / 60);
    const m = v.minutes_to_full % 60;
    const eta = h ? `${h} h ${m} min` : `${m} min`;
    return `${eta} to ${v.limit}%  ·  ${nfmt(v.energy_added_kwh, 1)} kWh added`;
  }
  if (v.usable_soc != null && v.soc != null && v.usable_soc < v.soc) {
    return `${v.usable_soc}% usable — the pack is cold`;
  }
  return v.version ? `Software ${v.version}` : "";
}

function renderTiles(v) {
  const host = $("tiles");
  host.replaceChildren();
  const tiles = [
    ["Inside", celsius(v.inside_c)],
    ["Outside", celsius(v.outside_c)],
    ["Locked", v.locked == null ? "—" : v.locked ? "Yes" : "No"],
    ["Sentry", v.sentry == null ? "—" : v.sentry ? "On" : "Off"],
    // The closest thing to a camera the API offers. There is no footage endpoint.
    ["Dashcam", v.dashcam || "—"],
    ["Charge port", v.port_open ? "Open" : "Closed"],
  ];

  const openDoors = Object.entries(v.doors || {}).filter(([, open]) => open);
  const openWindows = Object.entries(v.windows || {}).filter(([, open]) => open);
  const openTrunks = Object.entries(v.trunks || {}).filter(([, open]) => open);
  const openings = openDoors.length + openWindows.length + openTrunks.length;
  tiles.push(["Open", openings ? String(openings) : "All closed"]);

  const lowTyre = Object.entries(v.tpms_warn || {}).find(([, warn]) => warn);
  if (Object.keys(v.tpms_bar || {}).length) {
    tiles.push(["Tyres", lowTyre ? `Low: ${lowTyre[0].toUpperCase()}` : "OK"]);
  }

  for (const [label, value] of tiles) {
    const tile = document.createElement("div");
    tile.className = "live-tile";
    const l = document.createElement("span");
    l.className = "tile-label";
    l.textContent = label;
    const val = document.createElement("span");
    val.className = "tile-value";
    val.textContent = value;
    tile.append(l, val);
    host.append(tile);
  }
}
```

- [ ] **Step 6: Add the map to `car.js`**

```javascript
function renderMap(v) {
  const note = $("map-note");
  if (v.lat == null || v.lon == null) {
    note.textContent = "Location unavailable — needs the vehicle_location scope.";
    $("map").classList.add("empty");
    return;
  }
  note.textContent = v.route
    ? `Navigating to ${v.route.destination || "destination"} · ${Math.round(v.route.minutes)} min`
    : "";
  $("map").classList.remove("empty");

  if (!state.map) {
    state.map = L.map("map", { zoomControl: true, attributionControl: true })
      .setView([v.lat, v.lon], 15);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(state.map);
    // Leaflet resolves its default icons relative to the CSS by default.
    L.Icon.Default.prototype.options.imagePath = "/vendor/images/";
    state.marker = L.marker([v.lat, v.lon]).addTo(state.map);
  } else {
    state.marker.setLatLng([v.lat, v.lon]);
    state.map.setView([v.lat, v.lon], state.map.getZoom());
  }

  state.marker.bindPopup(
    v.shift && v.shift !== "P" ? `Moving · ${v.speed_mph} mph` : "Parked"
  );
  // A map created inside a hidden element measures zero; recompute once shown.
  setTimeout(() => state.map.invalidateSize(), 0);
}
```

- [ ] **Step 7: Add the boot sequence to `car.js`**

```javascript
async function refresh() {
  try {
    const [body, health] = await Promise.all([
      api("/api/car/state"),
      api("/api/car/health"),
    ]);
    state.health = health;
    $("error-bar").hidden = true;
    renderState(body);
    renderFooter(health);
  } catch (err) {
    if (err.status === 401) { showGate(err.message); return; }
    $("error-bar").hidden = false;
    $("error-bar").textContent = err.message;
  }
}

function renderFooter(health) {
  const c = health.collector;
  $("foot-collector").textContent = c.running
    ? `Collector running · last sample ${ago(Math.floor(Date.now() / 1000) - c.last_sample)}`
    : "Collector not running — SoC history has gaps";
  $("foot-calls").textContent = `${health.calls_today} samples in 24 h`;
  $("foot-version").textContent = state.body?.view?.version
    ? `Software ${state.body.view.version}` : "";
}

function showGate(message) {
  $("car-app").hidden = true;
  $("gate").hidden = false;
  gate(state.config, message);
}

async function main() {
  initTheme(() => renderSoc());
  state.config = await api("/api/config");
  if (!state.config.configured || !state.config.authenticated) { showGate(); return; }

  $("gate").hidden = true;
  $("car-app").hidden = false;

  $("wake").addEventListener("click", async () => {
    $("wake").disabled = true;
    try {
      await api("/api/car/wake", { method: "POST" });
      toast("Wake sent — the car takes up to a minute to come online.");
      setTimeout(refresh, 15000);
    } catch (err) {
      toast(err.message, false);
    } finally {
      $("wake").disabled = false;
    }
  });

  initRanges();
  await refresh();
  await loadHistory();

  // Live values go stale fast. This is a read against our own snapshot when the
  // car is asleep, so it costs nothing while parked.
  setInterval(() => { if (!document.hidden) refresh(); }, 60_000);
  addEventListener("resize", () => renderSoc());
}

main().catch((err) => showGate(err.message));
```

`initRanges`, `loadHistory`, `renderSoc`, and `renderControlsAvailability` arrive in Tasks 9 and 12. To keep this task independently runnable, add temporary no-op definitions and delete them as those tasks land:

```javascript
function initRanges() {}
async function loadHistory() {}
function renderSoc() {}
function renderControlsAvailability() {}
```

- [ ] **Step 8: Add the car page styles**

Append to `static/styles.css`. These use the file's existing custom properties —
`--surface`, `--plane`, `--ink`, `--muted`, `--grid`, `--baseline`, `--border`,
`--radius`, `--solar`, `--battery`, `--import`, `--export`, `--home` — which are
already defined for both light and dark, so no new colors are introduced.

```css
/* ---------------------------------------------------------------- car page */

.hero { display: flex; gap: 2rem; align-items: center; flex-wrap: wrap;
        padding: 1.5rem 0; }
.hero-ring { position: relative; width: 160px; height: 160px; flex: none; }
.hero-ring svg { width: 100%; height: 100%; transform: rotate(-90deg); }
.ring-track { fill: none; stroke: var(--grid); stroke-width: 10; }
.ring-fill  { fill: none; stroke: var(--battery); stroke-width: 10;
              stroke-linecap: round; transition: stroke-dasharray .6s ease; }
.ring-fill.charging { stroke: var(--solar); }
.ring-limit { fill: none; stroke: var(--ink); stroke-width: 12; opacity: .55; }
.hero-ring-label { position: absolute; inset: 0; display: grid; place-content: center;
                   font-size: 2.25rem; font-weight: 650; letter-spacing: -.02em; }
.hero-ring-label .unit { font-size: 1.1rem; font-weight: 500; opacity: .6; }
.hero-facts { flex: 1 1 16rem; }
.hero-headline { font-size: 1.5rem; font-weight: 600; margin: 0 0 .25rem; }
.hero-dl { display: flex; gap: 2rem; margin: 1rem 0 0; flex-wrap: wrap; }
.hero-dl dt { font-size: .8rem; color: var(--muted); margin: 0; }
.hero-dl dd { margin: .15rem 0 0; font-size: 1.05rem; font-variant-numeric: tabular-nums; }

/* Staleness reads as information, not as an error — it is the normal state. */
.stale-bar { background: var(--plane); color: var(--ink); border: 1px solid var(--border);
             border-left: 3px solid var(--solar);
             padding: .6rem .9rem; border-radius: var(--radius); margin-bottom: 1rem;
             font-size: .9rem; }

.map { height: 320px; border-radius: var(--radius); overflow: hidden;
       border: 1px solid var(--border); }
.map.empty { display: grid; place-content: center; background: var(--plane);
             color: var(--muted); }
.map.empty::after { content: "No location"; }
/* Leaflet panes stack above the tooltip layer by default. */
.leaflet-pane { z-index: 1; }

.foot { display: flex; gap: 1.5rem; flex-wrap: wrap; padding: 2rem 0 1rem;
        border-top: 1px solid var(--border); margin-top: 2rem; }

.toast { position: fixed; left: 50%; bottom: 2rem; transform: translateX(-50%);
         background: var(--ink); color: var(--surface); padding: .7rem 1.1rem;
         border-radius: var(--radius); font-size: .9rem; z-index: 1000; max-width: 90vw;
         box-shadow: var(--shadow); }
.toast.bad { background: var(--import); color: #fff; }
```

- [ ] **Step 9: Verify in the browser**

```bash
lsof -ti:8000 | xargs kill -9 2>/dev/null
cd /Users/d/Code/tesla_automation && DEMO=1 .venv/bin/python app.py > /tmp/demo.log 2>&1 &
sleep 2
```

Open http://localhost:8000/car.html and confirm: the ring shows the demo SoC with a limit notch, the headline reads "Charging…" or "Parked", tiles populate, the map centers on Denver with a marker, no console errors, dark mode flips, and the Solar link navigates back.

- [ ] **Step 10: Commit**

```bash
git add static/car.html static/car.js static/vendor static/styles.css static/index.html
git commit -m "feat: car page shell with charge hero, status tiles, and map"
```

---

### Task 9: State-of-charge chart with selectable timeframes

**Files:**
- Modify: `static/car.js` (replace the `initRanges`, `loadHistory`, `renderSoc` no-ops)

**Interfaces:**
- Consumes: `/api/car/history` from Task 7; `chartFrame`, `niceTicks`, `el`, `attachCrosshair`, `showTip` from Task 6.
- Produces: no exports; `renderSoc()` draws into `#soc-chart` from `state.history`.

- [ ] **Step 1: Replace the no-ops in `car.js`**

```javascript
function initRanges() {
  const host = $("soc-range");
  host.replaceChildren();
  for (const r of RANGES) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.role = "tab";
    btn.textContent = r.label;
    btn.setAttribute("aria-selected", String(r.key === state.range));
    btn.addEventListener("click", () => {
      state.range = r.key;
      for (const b of host.children) b.setAttribute("aria-selected", String(b === btn));
      loadHistory();
    });
    host.append(btn);
  }
}

async function loadHistory() {
  try {
    state.history = await api(`/api/car/history?range=${state.range}`);
    renderSoc();
  } catch (err) {
    $("soc-note").textContent = err.message;
  }
}
```

- [ ] **Step 2: Add the renderer**

The one thing this chart must get right is honesty about holes. A sleeping car emits nothing, so a naive polyline invents a smooth slope across an eight-hour gap. Solid segments are measured; dashed segments are inferred.

```javascript
function renderSoc() {
  const host = $("soc-chart");
  const data = state.history;
  if (!host) return;

  if (!data || !data.rows.length) {
    host.replaceChildren();
    const since = data?.since;
    $("soc-note").textContent = since
      ? "No samples in this range."
      : "No history yet — the collector starts recording from now on.";
    return;
  }

  const rows = data.rows;
  const socs = rows.map((r) => r.soc);
  // SoC is a percentage; anchoring to 0-100 stops a flat day looking dramatic.
  const lo = Math.max(0, Math.min(...socs) - 10);
  const hi = Math.min(100, Math.max(...socs) + 10);
  const { ticks } = niceTicks(lo, hi, 4);
  const { svg, plotW, plotH, yScale } =
    chartFrame(host, { yLo: lo, yHi: hi, yTicks: ticks, unit: "%" });

  const t0 = rows[0].ts;
  const span = Math.max(1, rows[rows.length - 1].ts - t0);
  const xScale = (i) => PAD.left + ((rows[i].ts - t0) / span) * plotW;

  // Charging stretches get a subtle band, so "why did it go up" is answered
  // without reading the tooltip.
  let runStart = null;
  rows.forEach((r, i) => {
    if (r.charging && runStart === null) runStart = i;
    if ((!r.charging || i === rows.length - 1) && runStart !== null) {
      const x1 = xScale(runStart);
      const x2 = xScale(i);
      if (x2 - x1 > 1) {
        svg.append(el("rect", {
          x: x1, y: PAD.top, width: x2 - x1, height: plotH,
          fill: COLOR("solar"), opacity: 0.12,
        }));
      }
      runStart = null;
    }
  });

  // Two paths: measured (solid) and inferred-across-sleep (dashed).
  let solid = "";
  let dashed = "";
  rows.forEach((r, i) => {
    const x = xScale(i);
    const y = yScale(r.soc);
    if (i === 0) { solid += `M${x},${y}`; return; }
    if (r.gap) {
      const px = xScale(i - 1);
      const py = yScale(rows[i - 1].soc);
      dashed += `M${px},${py}L${x},${y}`;
      solid += `M${x},${y}`;
    } else {
      solid += `L${x},${y}`;
    }
  });

  if (dashed) {
    svg.append(el("path", {
      d: dashed, fill: "none", stroke: COLOR("battery"), "stroke-width": 2,
      "stroke-dasharray": "4 4", opacity: 0.45,
    }));
  }
  svg.append(el("path", {
    d: solid, fill: "none", stroke: COLOR("battery"), "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));

  // X labels: dates for multi-day ranges, clock time for a single day.
  const multiDay = span > 36 * 3600;
  const step = Math.max(1, Math.ceil(rows.length / Math.max(2, Math.floor(plotW / 70))));
  rows.forEach((r, i) => {
    if (i % step !== 0) return;
    const d = new Date(r.ts * 1000);
    const label = multiDay
      ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
      : d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    const text = el("text", {
      x: xScale(i), y: PAD.top + plotH + 20, "text-anchor": "middle", class: "tick-label",
    });
    text.textContent = label;
    svg.append(text);
  });

  attachCrosshair(
    svg, rows, xScale, plotW, plotH,
    (r) => {
      const out = [{ name: "Charge", value: `${r.soc}%`, color: COLOR("battery") }];
      // usable and displayed SoC diverge when the pack is cold; showing both on
      // the line would read as a rendering bug, so it lives here.
      if (r.usable_soc != null && r.usable_soc !== r.soc) {
        out.push({ name: "Usable", value: `${r.usable_soc}%`, color: COLOR("export") });
      }
      if (r.charging) out.push({ name: "Charging", value: "yes", color: COLOR("solar") });
      if (r.gap) out.push({ name: "Gap", value: "car asleep", color: COLOR("baseline") });
      return out;
    },
    (r) => new Date(r.ts * 1000).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    })
  );

  const since = data.since ? new Date(data.since * 1000) : null;
  const gaps = rows.filter((r) => r.gap).length;
  $("soc-note").textContent =
    (since ? `Recording since ${since.toLocaleDateString()}. ` : "") +
    (gaps ? `Dashed segments are periods the car was asleep (${gaps}).` : "");
}
```

- [ ] **Step 3: Verify against demo data**

Reload http://localhost:8000/car.html. Confirm:
- The chart renders with a visible line.
- Switching 24h / 7 days / 30 days / 90 days / All re-renders each time.
- Dashed segments appear — the demo history deliberately contains sleep gaps.
- Charging stretches show a faint band.
- The tooltip follows the pointer and shows Charge, and Usable when it differs.
- The note under the chart names the recording start and the gap count.
- Dark mode flips and the chart re-renders with dark hues.

- [ ] **Step 4: Verify the empty state**

```bash
curl -s "localhost:8000/api/car/history?range=24h" | head -c 120
```

Then temporarily set `state.history = { rows: [], since: null }` in the browser console and call `renderSoc()`. Expected: the chart clears and the note reads "No history yet — the collector starts recording from now on." Reload to restore.

- [ ] **Step 5: Commit**

```bash
git add static/car.js
git commit -m "feat: state-of-charge chart with selectable ranges and honest sleep gaps"
```

---

### Task 10: Command signing — TLS cert, virtual key, proxy

Setup task. No application code; it makes Task 11 possible. The Go binaries are already built at `~/go/bin` (`tesla-http-proxy`, `tesla-control`, `tesla-keygen`).

**Files:**
- Create: `keys/tls-cert.pem`, `keys/tls-key.pem` (both gitignored)
- Create: `com.tenxcious.tesla-proxy.plist`

**Interfaces:**
- Consumes: `keys/private-key.pem` — the **same** pair registered at `tenxcious.com`. Using a different key makes every command fail at the car, and Tesla does not support keypair rotation.
- Produces: a TLS listener on `https://localhost:4443` accepting the Fleet API command surface.

- [ ] **Step 1: Generate the proxy's TLS certificate**

This is the proxy's *server* certificate and is unrelated to the command-signing key. It is deliberately a different curve — the proxy refuses to start if you hand it a recycled P-256 command key.

```bash
cd /Users/d/Code/tesla_automation
openssl req -x509 -nodes -newkey ec \
  -pkeyopt ec_paramgen_curve:secp384r1 -pkeyopt ec_param_enc:named_curve \
  -subj '/CN=localhost' \
  -keyout keys/tls-key.pem -out keys/tls-cert.pem -sha256 -days 3650 \
  -addext "extendedKeyUsage = serverAuth" \
  -addext "keyUsage = digitalSignature, keyCertSign, keyAgreement" \
  -addext "subjectAltName = DNS:localhost,IP:127.0.0.1"
chmod 600 keys/tls-key.pem
ls -la keys/
```

- [ ] **Step 2: Start the proxy and confirm it accepts the partner key**

```bash
~/go/bin/tesla-http-proxy \
  -tls-key /Users/d/Code/tesla_automation/keys/tls-key.pem \
  -cert /Users/d/Code/tesla_automation/keys/tls-cert.pem \
  -key-file /Users/d/Code/tesla_automation/keys/private-key.pem \
  -host localhost -port 4443 -verbose
```

Expected log lines:
```
[debug] Verified that TLS key is not a recycled command-authentication key, because it is not NIST P256.
[info ] Listening on localhost:4443
```

Leave it running in one terminal for the next steps.

- [ ] **Step 3: Pair the virtual key with the car — user action**

The owner must do this on their phone. The car must be online.

1. Open `https://tesla.com/_ak/tenxcious.com` on the phone with the Tesla app (4.27.3+) installed.
2. Approve adding the key to "Stallion".

This works because the public half is already served at
`https://tenxcious.com/.well-known/appspecific/com.tesla.3p.public-key.pem`, which the app fetches at pairing time.

- [ ] **Step 4: Verify the pairing from the API**

```bash
cd /Users/d/Code/tesla_automation && .venv/bin/python -c "
import asyncio, json
from config import settings
from tesla import TeslaClient
async def main():
    c = TeslaClient(settings)
    vin = await c.resolve_vin()
    s = await c.fleet_status([vin])
    print('paired VINs :', s.get('key_paired_vins'))
    print('signing req :', s.get('vehicle_command_protocol_required'))
    print('keys on car :', s.get('total_number_of_keys'))
    await c.aclose()
asyncio.run(main())
"
```

Expected: `key_paired_vins` contains `5YJSA1E5XNF477026`. If it is empty, the pairing did not complete — repeat Step 3 with the car awake.

- [ ] **Step 5: Send one real signed command end to end**

`flash_lights` is the safest possible proof: visible, harmless, and reversible by doing nothing. The car must be awake and in park.

```bash
cd /Users/d/Code/tesla_automation
TOKEN=$(.venv/bin/python -c "
import asyncio
from config import settings
from tesla import TeslaClient
async def m():
    c = TeslaClient(settings); print(await c._access_token()); await c.aclose()
asyncio.run(m())")
curl -sS --cacert keys/tls-cert.pem \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  --data '{}' \
  "https://localhost:4443/api/1/vehicles/5YJSA1E5XNF477026/command/flash_lights"
```

Expected: `{"response":{"result":true,"reason":""}}` and the headlights flash.

Failure decoder:
- `"your public key has not been paired with the vehicle"` → Step 3 did not take.
- HTTP 408 → the car is asleep; wake it from the app and retry.
- `expected 17-character VIN in path` → a numeric Fleet id was used.
- HTTP 403 mentioning the command protocol → the token is missing `vehicle_cmds`; redo Task 1.

- [ ] **Step 6: Run the proxy under launchd**

Create `com.tenxcious.tesla-proxy.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.tenxcious.tesla-proxy</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/d/go/bin/tesla-http-proxy</string>
    <string>-tls-key</string><string>/Users/d/Code/tesla_automation/keys/tls-key.pem</string>
    <string>-cert</string><string>/Users/d/Code/tesla_automation/keys/tls-cert.pem</string>
    <string>-key-file</string><string>/Users/d/Code/tesla_automation/keys/private-key.pem</string>
    <string>-host</string><string>localhost</string>
    <string>-port</string><string>4443</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>/Users/d/Code/tesla_automation/proxy.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/d/Code/tesla_automation/proxy.log</string>
</dict>
</plist>
```

Add `proxy.log` to `.gitignore`, then:

```bash
# Stop the foreground proxy from Step 2 first (Ctrl-C in its terminal).
cp /Users/d/Code/tesla_automation/com.tenxcious.tesla-proxy.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.tenxcious.tesla-proxy.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.tenxcious.tesla-proxy.plist
sleep 3
launchctl list | grep tesla-proxy
lsof -nP -iTCP:4443 -sTCP:LISTEN
```

Expected: the label is listed and something is listening on 4443.

- [ ] **Step 7: Commit**

```bash
git add com.tenxcious.tesla-proxy.plist .gitignore
git commit -m "feat: run the Tesla command-signing proxy under launchd"
```

---

### Task 11: Command catalog and dispatch

**Files:**
- Create: `commands.py`
- Create: `tests/test_commands.py`
- Modify: `car_routes.py` (add `/commands` and `/command/{id}`)
- Modify: `tesla.py` (add `TeslaClient.command`)

**Interfaces:**
- Consumes: the proxy from Task 10.
- Produces:
  - `commands.CATALOG: list[dict]` — each `{"id", "label", "group", "risk", "confirm": bool, "params": [...], "needs": [...]}`; a param is `{"name", "type": "int"|"float"|"bool"|"enum", "min", "max", "options", "label", "default"}`.
  - `commands.GROUPS: list[str]` — display order.
  - `commands.find(cmd_id) -> dict | None`
  - `commands.validate(spec, payload) -> dict` — coerced body; raises `ValueError`.
  - `commands.interpret(status: int, body: dict) -> dict` → `{"ok": bool, "message": str, "reason": str}`
  - `TeslaClient.command(vin, name, body) -> tuple[int, dict]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_commands.py`:

```python
import pytest

import commands


def test_catalog_ids_are_unique_and_grouped():
    ids = [c["id"] for c in commands.CATALOG]
    assert len(ids) == len(set(ids))
    for c in commands.CATALOG:
        assert c["group"] in commands.GROUPS


def test_commands_broken_through_the_proxy_are_excluded():
    """These return 400 invalid_command through Tesla's own proxy. Shipping
    them would put permanently-broken buttons on the page."""
    broken = {
        "sun_roof_control", "navigation_gps_request", "navigation_sc_request",
        "navigation_waypoints_request", "upcoming_calendar_entries",
        "update_calendar_entries", "remote_boombox",
        "remote_steering_wheel_heat_level_request",
        "remote_auto_steering_wheel_heat_climate_request",
    }
    assert not broken & {c["id"] for c in commands.CATALOG}


def test_risky_commands_require_confirmation():
    for cid in ("door_unlock", "actuate_trunk", "window_control", "set_valet_mode"):
        assert commands.find(cid)["confirm"] is True


def test_validate_coerces_and_bounds_charge_limit():
    spec = commands.find("set_charge_limit")
    assert commands.validate(spec, {"percent": "80"}) == {"percent": 80}
    with pytest.raises(ValueError):
        commands.validate(spec, {"percent": 20})
    with pytest.raises(ValueError):
        commands.validate(spec, {"percent": 101})


def test_validate_rejects_a_missing_required_param():
    with pytest.raises(ValueError):
        commands.validate(commands.find("set_charge_limit"), {})


def test_validate_emits_real_json_types_not_strings():
    """The proxy's getBool demands JSON true/false and getNumber a JSON number;
    {"on": "true"} is rejected."""
    body = commands.validate(commands.find("set_sentry_mode"), {"on": "true"})
    assert body["on"] is True
    assert isinstance(body["on"], bool)


def test_interpret_success():
    r = commands.interpret(200, {"response": {"result": True, "reason": ""}})
    assert r["ok"] is True


def test_interpret_treats_benign_reasons_as_success():
    for reason in ("already_set", "not_charging", "is_charging", "complete", "requested"):
        r = commands.interpret(200, {"response": {"result": False, "reason": reason}})
        assert r["ok"] is True, reason


def test_interpret_real_failure_is_not_ok_and_keeps_the_reason():
    r = commands.interpret(200, {"response": {"result": False, "reason": "car_wash"}})
    assert r["ok"] is False
    assert "car_wash" in r["reason"]


def test_interpret_strips_the_proxy_prefix():
    r = commands.interpret(200, {"response": {
        "result": False, "reason": "car could not execute command: vehicle is in park"}})
    assert r["reason"] == "vehicle is in park"


def test_interpret_unpaired_key_gets_an_actionable_message():
    r = commands.interpret(200, {"response": None,
                                 "error": "your public key has not been paired with the vehicle"})
    assert r["ok"] is False
    assert "pair" in r["message"].lower()


def test_interpret_408_is_asleep():
    r = commands.interpret(408, {})
    assert r["ok"] is False
    assert "asleep" in r["message"].lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_commands.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'commands'`

- [ ] **Step 3: Write `commands.py`**

Every entry below is taken from `docs/tesla-field-reference.md` §4. The commands Tesla's own proxy rejects are deliberately absent.

```python
"""Command catalog, validation, and result interpretation.

Three facts shape this module:
  * HTTP 200 does not mean the car did anything. Parse `response.result`.
  * `reason` is not a stable enum on the signed path — the proxy prefixes it
    with "car could not execute command: ". Strip, then treat as opaque text.
  * Several documented commands return 400 invalid_command through Tesla's own
    proxy. They are excluded rather than shipped broken.
"""
from __future__ import annotations

from typing import Any

GROUPS = ["Charging", "Climate", "Access & security", "More"]

# The car reporting "nothing to do" is a success from the user's point of view.
BENIGN_REASONS = {"already_set", "not_charging", "requested", "is_charging",
                  "complete", "already open", "already closed",
                  "already on", "already off", "already_max_range",
                  "already_standard"}

_PROXY_PREFIXES = ("car could not execute command: ",
                   "vcsec could not execute command: ")


def _cmd(cid, label, group, params=(), confirm=False, needs=(), risk="normal"):
    return {"id": cid, "label": label, "group": group, "params": list(params),
            "confirm": confirm, "needs": list(needs), "risk": risk}


def _int(name, label, lo=None, hi=None, default=None):
    return {"name": name, "type": "int", "label": label,
            "min": lo, "max": hi, "default": default}


def _bool(name, label, default=False):
    return {"name": name, "type": "bool", "label": label, "default": default}


def _enum(name, label, options, default=None):
    return {"name": name, "type": "enum", "label": label,
            "options": list(options), "default": default}


def _str(name, label, secret=False):
    """`secret` makes the UI render a password field. PINs have no default —
    a defaulted PIN would be worse than no feature."""
    return {"name": name, "type": "string", "label": label,
            "secret": secret, "default": None}


CATALOG: list[dict[str, Any]] = [
    # ---- Charging ----
    _cmd("charge_start", "Start charging", "Charging"),
    _cmd("charge_stop", "Stop charging", "Charging"),
    _cmd("set_charge_limit", "Set charge limit", "Charging",
         # Tesla accepts an out-of-range percent, returns success, and silently
         # no-ops. Bounding it here is the only real validation.
         [_int("percent", "Limit %", 50, 100, 80)]),
    _cmd("set_charging_amps", "Set charging amps", "Charging",
         [_int("charging_amps", "Amps", 1, 48, 32)]),
    _cmd("charge_port_door_open", "Open charge port", "Charging"),
    _cmd("charge_port_door_close", "Close charge port", "Charging"),

    # ---- Climate ----
    _cmd("auto_conditioning_start", "Climate on", "Climate"),
    _cmd("auto_conditioning_stop", "Climate off", "Climate"),
    _cmd("set_temps", "Set temperature", "Climate",
         [{"name": "driver_temp", "type": "float", "label": "Driver °C",
           "min": 15, "max": 28, "default": 21},
          {"name": "passenger_temp", "type": "float", "label": "Passenger °C",
           "min": 15, "max": 28, "default": 21}]),
    _cmd("set_preconditioning_max", "Defrost (max)", "Climate",
         [_bool("on", "On", True)]),
    # seat_position is 0-BASED here and 1-based on the cooler/auto commands.
    # Off-by-one silently heats the wrong seat.
    _cmd("remote_seat_heater_request", "Seat heater", "Climate",
         [_enum("seat_position", "Seat",
                [{"value": 0, "label": "Front left"}, {"value": 1, "label": "Front right"},
                 {"value": 2, "label": "Rear left"}, {"value": 4, "label": "Rear center"},
                 {"value": 5, "label": "Rear right"}], 0),
          _int("level", "Level", 0, 3, 0)],
         needs=["climate_on"]),
    _cmd("remote_steering_wheel_heater_request", "Steering wheel heater", "Climate",
         [_bool("on", "On", True)], needs=["climate_on"]),
    _cmd("set_climate_keeper_mode", "Climate keeper", "Climate",
         [_enum("climate_keeper_mode", "Mode",
                [{"value": 0, "label": "Off"}, {"value": 1, "label": "Keep"},
                 {"value": 2, "label": "Dog"}, {"value": 3, "label": "Camp"}], 0)]),
    _cmd("set_cabin_overheat_protection", "Cabin overheat protection", "Climate",
         [_bool("on", "On", True), _bool("fan_only", "Fan only", False)]),

    # ---- Access & security ----
    _cmd("door_lock", "Lock", "Access & security"),
    _cmd("door_unlock", "Unlock", "Access & security", confirm=True, risk="high"),
    _cmd("actuate_trunk", "Open trunk", "Access & security",
         [_enum("which_trunk", "Which",
                [{"value": "rear", "label": "Rear"}, {"value": "front", "label": "Frunk"}],
                "rear")],
         confirm=True, risk="high"),
    _cmd("set_sentry_mode", "Sentry mode", "Access & security",
         [_bool("on", "On", True)]),
    _cmd("flash_lights", "Flash lights", "Access & security"),
    _cmd("honk_horn", "Honk horn", "Access & security", confirm=True),

    # ---- More ----
    # The proxy ignores lat/lon entirely, but the field reference documents them
    # as a proximity proof, so they are omitted rather than faked.
    _cmd("window_control", "Windows", "More",
         [_enum("command", "Action",
                [{"value": "vent", "label": "Vent"}, {"value": "close", "label": "Close"}],
                "close")],
         confirm=True, risk="high"),
    _cmd("set_valet_mode", "Valet mode", "More",
         [_bool("on", "On", True)], confirm=True, risk="high"),
    # Speed Limit Mode. Tesla documents no range for limit_mph and the proxy
    # does not validate it; 50-90 is the range the car's own UI offers.
    _cmd("speed_limit_activate", "Speed limit on", "More",
         [_str("pin", "4-digit PIN", secret=True)], confirm=True, risk="high"),
    _cmd("speed_limit_deactivate", "Speed limit off", "More",
         [_str("pin", "4-digit PIN", secret=True)], confirm=True),
    _cmd("speed_limit_set_limit", "Speed limit value", "More",
         [_int("limit_mph", "mph", 50, 90, 75)], confirm=True),
    _cmd("remote_start_drive", "Remote start", "More", confirm=True, risk="high"),
    _cmd("media_toggle_playback", "Play / pause", "More", needs=["user_present"]),
    _cmd("media_next_track", "Next track", "More", needs=["user_present"]),
    _cmd("media_prev_track", "Previous track", "More", needs=["user_present"]),
    _cmd("adjust_volume", "Volume", "More",
         [{"name": "volume", "type": "float", "label": "0-10",
           "min": 0, "max": 10, "default": 5}], needs=["user_present"]),
    _cmd("trigger_homelink", "HomeLink", "More", confirm=True),
    _cmd("schedule_software_update", "Install update", "More",
         [_int("offset_sec", "Delay (s)", 0, 86400, 0)], confirm=True),
    _cmd("cancel_software_update", "Cancel update", "More"),
]

_BY_ID = {c["id"]: c for c in CATALOG}


def find(cmd_id: str) -> dict[str, Any] | None:
    return _BY_ID.get(cmd_id)


def validate(spec: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce to real JSON types and bound-check.

    The proxy's getBool demands JSON true/false and getNumber a JSON number —
    {"on": "true"} and {"percent": "80"} are both rejected upstream, so the
    coercion has to happen here."""
    body: dict[str, Any] = {}
    for param in spec["params"]:
        name = param["name"]
        if name not in payload or payload[name] is None:
            if param.get("default") is None:
                raise ValueError(f"missing required parameter {name!r}")
            body[name] = param["default"]
            continue

        raw = payload[name]
        kind = param["type"]
        if kind == "bool":
            if isinstance(raw, bool):
                body[name] = raw
            elif str(raw).lower() in {"true", "1", "yes"}:
                body[name] = True
            elif str(raw).lower() in {"false", "0", "no"}:
                body[name] = False
            else:
                raise ValueError(f"{name} must be a boolean")
        elif kind in {"int", "float"}:
            try:
                value = int(raw) if kind == "int" else float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be a number") from None
            if param.get("min") is not None and value < param["min"]:
                raise ValueError(f"{name} must be at least {param['min']}")
            if param.get("max") is not None and value > param["max"]:
                raise ValueError(f"{name} must be at most {param['max']}")
            body[name] = value
        elif kind == "string":
            text = str(raw).strip()
            if not text:
                raise ValueError(f"{name} is required")
            body[name] = text
        elif kind == "enum":
            allowed = [o["value"] for o in param["options"]]
            value = raw
            if value not in allowed:
                # Enum values arrive as strings over HTTP even when they are ints.
                for option in allowed:
                    if str(option) == str(raw):
                        value = option
                        break
                else:
                    raise ValueError(f"{name} must be one of {allowed}")
            body[name] = value
        else:
            body[name] = raw
    return body


def interpret(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """Turn a proxy response into something a human can act on."""
    body = body or {}
    error = (body.get("error") or "").strip()

    if error:
        if "has not been paired" in error or "UNKNOWN_KEY_ID" in error:
            return {"ok": False, "reason": error,
                    "message": "The car has not been paired with this app's key. "
                               "Open tesla.com/_ak/tenxcious.com on your phone."}
        if error == "invalid_command":
            return {"ok": False, "reason": error,
                    "message": "The signing proxy does not support this command."}

    if status == 408:
        return {"ok": False, "reason": "asleep",
                "message": "The car is asleep. Wake it, then try again."}
    if status == 403:
        return {"ok": False, "reason": "forbidden",
                "message": "Not permitted — the token is missing a command scope."}
    if status == 429:
        return {"ok": False, "reason": "rate_limited",
                "message": "Rate limited by Tesla. These limits are shared with "
                           "every app authorized on this account."}

    response = body.get("response")
    if not isinstance(response, dict):
        return {"ok": False, "reason": error or f"http_{status}",
                "message": error or f"Unexpected response ({status})."}

    reason = (response.get("reason") or "").strip()
    for prefix in _PROXY_PREFIXES:
        if reason.startswith(prefix):
            reason = reason[len(prefix):]
            break

    if response.get("result") is True:
        return {"ok": True, "reason": reason, "message": "Done"}
    if reason in BENIGN_REASONS:
        return {"ok": True, "reason": reason, "message": reason.replace("_", " ").capitalize()}
    return {"ok": False, "reason": reason or "rejected",
            "message": f"The car declined: {reason or 'no reason given'}"}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_commands.py -v`
Expected: 12 passed

- [ ] **Step 5: Add the command client to `tesla.py`**

```python
    async def command(
        self, vin: str, name: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Signed commands via the local proxy.

        Only /command/* goes through the proxy — reads stay direct so a stopped
        proxy costs us buttons, not data. The proxy's certificate is self-signed,
        so it is supplied as this client's CA bundle; verification stays ON."""
        if self._proxy is None:
            self._proxy = httpx.AsyncClient(
                verify=str(self.settings.proxy_cert), timeout=30.0
            )
        token = await self._access_token()
        url = f"{self.settings.proxy_url}/api/1/vehicles/{vin}/command/{name}"
        resp = await self._proxy.post(
            url, json=body,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"error": resp.text[:300]}
```

In `TeslaClient.__init__`, add `self._proxy: httpx.AsyncClient | None = None`, and in `aclose`:

```python
    async def aclose(self) -> None:
        await self._http.aclose()
        if self._proxy is not None:
            await self._proxy.aclose()
```

- [ ] **Step 6: Add the routes to `car_routes.py`**

```python
import commands as command_catalog
from fastapi import Body


@router.get("/commands")
async def car_commands() -> dict[str, Any]:
    """The UI is data-driven off this, so adding a command needs no JS change."""
    return {"groups": command_catalog.GROUPS, "commands": command_catalog.CATALOG}


@router.post("/command/{cmd_id}")
async def car_command(cmd_id: str, payload: dict[str, Any] = Body(default={})):
    spec = command_catalog.find(cmd_id)
    if spec is None:
        raise HTTPException(404, f"unknown command {cmd_id!r}")
    try:
        body = command_catalog.validate(spec, payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if DEMO:
        return {"ok": True, "reason": "", "message": f"Demo: {spec['label']} sent"}

    vin = await _vin()
    status, raw = await _client().command(vin, cmd_id, body)
    result = command_catalog.interpret(status, raw)

    # Command effects change state; drop the read cache so the next poll is fresh.
    _client().cache.clear()
    return result
```

- [ ] **Step 7: Verify against the demo server and a real command**

```bash
lsof -ti:8000 | xargs kill -9 2>/dev/null
cd /Users/d/Code/tesla_automation && DEMO=1 .venv/bin/python app.py > /tmp/demo.log 2>&1 &
sleep 2
curl -s localhost:8000/api/car/commands | head -c 300; echo
curl -s -X POST localhost:8000/api/car/command/set_charge_limit \
  -H 'Content-Type: application/json' -d '{"percent": 80}'; echo
curl -s -X POST localhost:8000/api/car/command/set_charge_limit \
  -H 'Content-Type: application/json' -d '{"percent": 20}'; echo
```

Expected: the catalog lists the groups; the first POST returns `ok: true`; the second returns HTTP 400 with "percent must be at least 50".

Then against the real car (proxy running, car awake):

```bash
lsof -ti:8000 | xargs kill -9 2>/dev/null
cd /Users/d/Code/tesla_automation && .venv/bin/python app.py > /tmp/app.log 2>&1 &
sleep 2
curl -s -X POST localhost:8000/api/car/command/flash_lights -d '{}'
```

Expected: `{"ok":true,"reason":"","message":"Done"}` and the lights flash.

- [ ] **Step 8: Commit**

```bash
git add commands.py tests/test_commands.py car_routes.py tesla.py
git commit -m "feat: signed command catalog, validation, and dispatch"
```

---

### Task 12: Controls UI

**Files:**
- Modify: `static/car.js` (replace the `renderControlsAvailability` no-op)
- Modify: `static/styles.css` (control styles)

**Interfaces:**
- Consumes: `/api/car/commands`, `/api/car/command/{id}`, `/api/car/health` from Tasks 7 and 11.
- Produces: no exports. `renderControls()` builds `#controls`; `renderControlsAvailability()` enables/disables against current state.

- [ ] **Step 1: Replace the no-op and build the control groups**

```javascript
async function loadCommands() {
  try {
    state.commands = await api("/api/car/commands");
    renderControls();
  } catch (err) {
    $("controls").textContent = err.message;
  }
}

function renderControls() {
  const host = $("controls");
  host.replaceChildren();
  const { groups, commands } = state.commands;

  const head = document.createElement("div");
  head.className = "card-head";
  const h2 = document.createElement("h2");
  h2.textContent = "Controls";
  const note = document.createElement("span");
  note.className = "muted small";
  note.id = "controls-note";
  head.append(h2, note);
  host.append(head);

  for (const group of groups) {
    const inGroup = commands.filter((c) => c.group === group);
    if (!inGroup.length) continue;

    const details = document.createElement("details");
    details.className = "control-group";
    // Charging is what the page is mostly for; the rest stay folded away.
    details.open = group === "Charging";
    const summary = document.createElement("summary");
    summary.textContent = group;
    details.append(summary);

    const grid = document.createElement("div");
    grid.className = "control-grid";
    for (const cmd of inGroup) grid.append(controlRow(cmd));
    details.append(grid);
    host.append(details);
  }
  renderControlsAvailability();
}

function controlRow(cmd) {
  const row = document.createElement("div");
  row.className = "control-row";
  row.dataset.cmd = cmd.id;

  const label = document.createElement("span");
  label.className = "control-label";
  label.textContent = cmd.label;
  row.append(label);

  const inputs = document.createElement("div");
  inputs.className = "control-inputs";
  const fields = {};

  for (const param of cmd.params) {
    let field;
    if (param.type === "enum") {
      field = document.createElement("select");
      for (const opt of param.options) {
        const o = document.createElement("option");
        o.value = String(opt.value);
        o.textContent = opt.label;
        field.append(o);
      }
      field.value = String(param.default);
    } else if (param.type === "bool") {
      field = document.createElement("select");
      for (const [v, t] of [["true", "On"], ["false", "Off"]]) {
        const o = document.createElement("option");
        o.value = v;
        o.textContent = t;
        field.append(o);
      }
      field.value = String(param.default);
    } else if (param.type === "string") {
      field = document.createElement("input");
      // PINs are never prefilled and never echoed.
      field.type = param.secret ? "password" : "text";
      field.autocomplete = "off";
      field.placeholder = param.label;
    } else {
      field = document.createElement("input");
      field.type = "number";
      if (param.min != null) field.min = param.min;
      if (param.max != null) field.max = param.max;
      field.value = param.default ?? "";
      field.step = param.type === "float" ? "0.5" : "1";
    }
    field.setAttribute("aria-label", param.label);
    field.className = "control-field";
    fields[param.name] = field;
    inputs.append(field);
  }

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = cmd.risk === "high" ? "btn danger" : "btn";
  btn.textContent = "Send";
  btn.addEventListener("click", () => send(cmd, fields, btn));
  inputs.append(btn);

  row.append(inputs);
  return row;
}
```

- [ ] **Step 2: Add sending, with confirmation on risky commands**

```javascript
async function send(cmd, fields, btn) {
  const payload = {};
  for (const [name, field] of Object.entries(fields)) payload[name] = field.value;

  if (cmd.confirm) {
    const asleep = state.body?.car_state !== "online";
    const extra = asleep ? "\n\nThe car is asleep — wake it first or this will fail." : "";
    if (!window.confirm(`${cmd.label}?${extra}`)) return;
  }

  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "…";
  try {
    const result = await api(`/api/car/command/${cmd.id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    toast(result.message, result.ok);
    // The car takes a moment to reflect the change; re-read rather than guess.
    if (result.ok) setTimeout(refresh, 3000);
  } catch (err) {
    toast(err.message, false);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function renderControlsAvailability() {
  const host = $("controls");
  if (!host || !state.commands) return;
  const health = state.health;
  const v = state.body?.view;
  const note = $("controls-note");

  // One blocking reason at a time, most fundamental first — a list of four
  // problems is less actionable than the one to fix now.
  let blocked = null;
  if (health && health.proxy === false) {
    blocked = "Signing proxy is not running — start it to enable controls.";
  } else if (health && health.key_paired === false) {
    blocked = "This app's key is not paired with the car. Open tesla.com/_ak/tenxcious.com on your phone.";
  } else if (health && health.missing_scopes?.length) {
    blocked = `Missing scope: ${health.missing_scopes.join(", ")}. Reconnect your Tesla account.`;
  }

  note.textContent = blocked || (state.body?.car_state === "online"
    ? "" : "Car is asleep — commands will wake it or fail.");

  for (const row of host.querySelectorAll(".control-row")) {
    const cmd = state.commands.commands.find((c) => c.id === row.dataset.cmd);
    let disabled = Boolean(blocked);
    let reason = blocked || "";

    // Seat and wheel heaters are rejected outright unless climate is already
    // running, so the UI says so rather than letting the car refuse.
    if (!disabled && cmd.needs.includes("climate_on") && v && !v.climate_any) {
      disabled = true;
      reason = "Turn climate on first.";
    }
    if (!disabled && cmd.needs.includes("user_present") && v && !v.user_present) {
      disabled = true;
      reason = "Only works when someone is in the car.";
    }
    if (!disabled && cmd.id === "set_sentry_mode" && v && v.sentry_available === false) {
      disabled = true;
      reason = "Sentry is not available on this car right now.";
    }

    row.classList.toggle("disabled", disabled);
    row.title = reason;
    for (const control of row.querySelectorAll("button, input, select")) {
      control.disabled = disabled;
    }
  }
}
```

- [ ] **Step 3: Call `loadCommands()` during boot**

In `main()`, after `initRanges();`:

```javascript
  await loadCommands();
```

Add `commands: null` to the `state` object at the top of `car.js`.

- [ ] **Step 4: Add the control styles**

Append to `static/styles.css`:

```css
.control-group { border-top: 1px solid var(--border); padding: .5rem 0; }
.control-group summary { cursor: pointer; font-weight: 600; padding: .5rem 0;
                         list-style: revert; }
.control-grid { display: grid; gap: .5rem; padding: .5rem 0 1rem; }
.control-row { display: flex; align-items: center; justify-content: space-between;
               gap: 1rem; flex-wrap: wrap; }
.control-row.disabled { opacity: .45; }
.control-label { font-size: .95rem; }
.control-inputs { display: flex; gap: .5rem; align-items: center; }
.control-field { padding: .35rem .5rem; font: inherit; font-size: .9rem;
                 border: 1px solid var(--border); border-radius: .35rem;
                 background: var(--surface); color: var(--ink); min-width: 5rem; }
.btn.danger { background: var(--import); }
```

- [ ] **Step 5: Verify in demo mode**

Reload http://localhost:8000/car.html. Confirm:
- Four groups render; Charging is open, the rest folded.
- Charge-limit accepts 80 and shows a toast; setting 20 shows an error toast.
- Unlock, trunk, windows, and valet prompt for confirmation.
- Seat heater is disabled with the tooltip "Turn climate on first." (demo climate is off).
- Media rows are disabled with "Only works when someone is in the car."
- Tab order reaches every control and Enter activates buttons.

- [ ] **Step 6: Verify the blocked states are honest**

```bash
launchctl unload ~/Library/LaunchAgents/com.tenxcious.tesla-proxy.plist
lsof -ti:8000 | xargs kill -9 2>/dev/null
cd /Users/d/Code/tesla_automation && .venv/bin/python app.py > /tmp/app.log 2>&1 &
sleep 2
curl -s localhost:8000/api/car/health
```

Reload the page: every control must be disabled and the note must read "Signing proxy is not running…". Then reload the proxy and confirm they re-enable.

```bash
launchctl load ~/Library/LaunchAgents/com.tenxcious.tesla-proxy.plist
```

- [ ] **Step 7: Commit**

```bash
git add static/car.js static/styles.css
git commit -m "feat: controls UI with risk confirmation and honest availability"
```

---

### Task 13: Doctor, docs, and the timezone fix

**Files:**
- Modify: `setup_tesla.py` (extend `doctor`)
- Modify: `SETUP.md`, `README.md`
- Modify: `.env` (timezone)

**Interfaces:**
- Consumes: everything above.
- Produces: `setup_tesla.py doctor` reporting the vehicle stack; documentation matching what shipped.

- [ ] **Step 1: Corroborate the timezone against the car's GPS**

**Already applied on 2026-07-25:** `.env` was changed from `America/Los_Angeles`
to `America/Denver` (the machine's zone) and the server restarted;
`/api/config` reports `"timezone": "America/Denver"`. This step now only
confirms it independently, since the car's coordinates are the authoritative
answer for where the solar site actually is:

```bash
cd /Users/d/Code/tesla_automation && .venv/bin/python -c "
import asyncio
from config import settings
from tesla import TeslaClient, VehicleAsleep
import vehicle
async def main():
    c = TeslaClient(settings)
    vin = await c.resolve_vin()
    try:
        v = vehicle.derive(await c.vehicle_data(vin))
        print('car at', v['lat'], v['lon'])
    except VehicleAsleep:
        print('asleep — falling back to the machine timezone')
    await c.aclose()
asyncio.run(main())
"
readlink /etc/localtime
```

A longitude near -105 confirms Mountain and no change is needed. If the car
turns out to be near -118 (Pacific), revert `.env` to `America/Los_Angeles` and
restart — the car's location beats the laptop's, since the laptop can travel and
the solar site cannot.

- [ ] **Step 2: Extend `doctor`**

`cmd_doctor` is synchronous and prints with the module's `OK` / `BAD` / `WARN`
glyphs, tracking an `ok` flag. The vehicle checks need async calls, so they go in
a helper invoked with `asyncio.run`.

Add this function above `cmd_doctor` in `setup_tesla.py`:

```python
async def _vehicle_checks() -> bool:
    """Vehicle-stack checks for `doctor`. Returns False if anything is broken."""
    import base64
    import json
    import socket
    import time
    from urllib.parse import urlparse

    from store import Store
    from tesla import TeslaAPIError, TeslaAuthError, TeslaClient

    ok = True
    client = TeslaClient(settings)
    try:
        try:
            token = await client._access_token()
        except TeslaAuthError:
            print(f"  {WARN} not logged in — skipping vehicle checks")
            return True

        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        scopes = json.loads(base64.urlsafe_b64decode(payload)).get("scp", [])
        for scope in ("vehicle_device_data", "vehicle_location",
                      "vehicle_cmds", "vehicle_charging_cmds"):
            if scope in scopes:
                print(f"  {OK} scope {scope}")
            else:
                print(f"  {BAD} scope {scope} missing — reconnect your Tesla account")
                ok = False

        try:
            vin = await client.resolve_vin()
            print(f"  {OK} vehicle {vin}")
        except (TeslaAPIError, TeslaAuthError) as exc:
            print(f"  {BAD} could not resolve a VIN: {exc}")
            return False

        try:
            status = await client.fleet_status([vin])
            if vin in (status.get("key_paired_vins") or []):
                print(f"  {OK} virtual key paired with the vehicle")
            else:
                print(f"  {BAD} virtual key NOT paired — open "
                      f"https://tesla.com/_ak/{settings.domain} on your phone")
                ok = False
            # Answers the billing question from the account rather than the docs.
            print(f"  {OK} discounted device data: "
                  f"{status.get('discounted_device_data')}")
            print(f"  {OK} command signing required: "
                  f"{status.get('vehicle_command_protocol_required')}")
            print(f"  {OK} firmware: {status.get('firmware_version')}")
        except TeslaAPIError as exc:
            print(f"  {WARN} fleet_status unavailable: {exc}")

        parsed = urlparse(settings.proxy_url)
        try:
            with socket.create_connection(
                (parsed.hostname or "localhost", parsed.port or 443), timeout=1
            ):
                print(f"  {OK} signing proxy reachable at {settings.proxy_url}")
        except OSError:
            print(f"  {BAD} signing proxy unreachable — launchctl load "
                  f"~/Library/LaunchAgents/com.tenxcious.tesla-proxy.plist")
            ok = False

        store = Store(settings.db_file)
        try:
            snap = store.snapshot(vin)
            if snap:
                age = int(time.time()) - snap["ts"]
                print(f"  {OK} last sample {age}s ago at {snap['view'].get('soc')}%")
                if age > 3600:
                    print(f"  {WARN} no sample in over an hour — is the collector running?")
            else:
                print(f"  {BAD} no samples recorded — launchctl load "
                      f"~/Library/LaunchAgents/com.tenxcious.tesla-collector.plist")
                ok = False
        finally:
            store.close()
    finally:
        await client.aclose()
    return ok
```

Then in `cmd_doctor`, immediately before the final summary line
(`print("\n" + ("All good." ...))`), add:

```python
    print("\nVehicle:")
    if not asyncio.run(_vehicle_checks()):
        ok = False
```

Add `import asyncio` to the imports at the top of `setup_tesla.py`.

- [ ] **Step 3: Run doctor**

```bash
cd /Users/d/Code/tesla_automation && .venv/bin/python setup_tesla.py doctor
```

Expected: all checks pass. Any failure prints its own remedy.

- [ ] **Step 4: Update the docs**

In `SETUP.md`, add a "Car page" section covering, in order: enabling the four vehicle scopes in the developer portal and waiting ~10 minutes; reconnecting the account; pairing the virtual key at `https://tesla.com/_ak/tenxcious.com`; building the proxy from a clone (`go install ./...` inside the repo — `go install …@latest` fails because the module has `replace` directives); generating the TLS cert; loading both launchd agents. State plainly that **no camera access exists in the Fleet API** and that **SoC history starts when the collector starts**.

In `README.md`, add the car page to the feature list and note the two background agents.

- [ ] **Step 5: Run the whole test suite**

```bash
cd /Users/d/Code/tesla_automation && .venv/bin/python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 6: Verify both pages one last time**

```bash
lsof -ti:8000 | xargs kill -9 2>/dev/null
cd /Users/d/Code/tesla_automation && .venv/bin/python app.py > /tmp/app.log 2>&1 &
sleep 2
curl -s localhost:8000/api/config
curl -s localhost:8000/api/car/health
```

Open both pages. Solar must be unchanged from the Task 6 baseline. Car must show live or last-known data with an honest age, a map, a SoC chart, and controls in their correct enabled state.

- [ ] **Step 7: Commit**

```bash
git add setup_tesla.py SETUP.md README.md .env.example
git commit -m "feat: extend doctor for the vehicle stack, update docs, fix timezone"
```

---

## Notes for the implementer

**Things that will bite if ignored:**

1. `app.mount("/", StaticFiles(...))` is a catch-all. Any router included after it is unreachable. Mount routers first.
2. The proxy demands a 17-character VIN. The numeric Fleet API id 404s with a message containing an upstream typo (`do not user Fleet API ID`).
3. `vehicle_data` returns 408 when asleep **and sometimes when `/vehicles` says online**. Treat 408 as a normal path everywhere.
4. Never blind-retry `media_toggle_playback`, `media_volume_up/down`, or any `add_`/`remove_` schedule command — they are not idempotent. Lock, unlock, `set_charge_limit`, `set_charging_amps`, and `set_temps` are safe to retry.
5. Tesla rate limits are per-account and **shared with every other app authorized on it**. A 429 may not be caused by this app.
6. Do not restart the FastAPI server while the user is mid-OAuth: the CSRF `state` is in memory and the callback will fail with `bad_state`.
7. `charge_limit_soc` and the ring notch: an out-of-range `set_charge_limit` returns **success** and silently does nothing. Always re-read `charge_limit_soc` after setting it.

**If a task's verification fails,** stop and fix it before moving on. Every task ends in a working state, and the solar dashboard working is the regression bar for all of them.
