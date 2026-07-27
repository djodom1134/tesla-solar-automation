# Solar-Aware Charging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Continuously adjust the car's charge rate to match measured solar export, so charging consumes electrons that would otherwise go to the grid.

**Architecture:** A closed-loop integral controller runs inside `collector.py` — the single launchd background agent — servoing `set_charging_amps` until the site's grid meter reads ~0. The control law is incremental (`error_w = -grid_w - margin_w`) because `grid_power` already contains the car's own draw and no site-side car-power channel exists on this account. All decision logic lives in pure functions; `collector.py` owns every side effect.

**Tech Stack:** Python 3.13, FastAPI, httpx, SQLite (WAL), vanilla JS + vendored Leaflet, launchd. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-26-solar-aware-charging-design.md` — read §3 before Task 6.

## Global Constraints

- **No new Python dependencies.** `requirements.txt` must not grow.
- **No frontend build step.** Vanilla JS, no npm, no bundler. Leaflet is already vendored at `static/vendor/`.
- **Never wake the car** except the single `stopped → charging` restart (Task 9).
- **Act only when `classify() == "home"`.** `unknown` freezes; it never triggers a command.
- **Read back after every write.** `result: true` is compatible with silent clamping. Integrate against `last_acknowledged_a`, never against what was requested.
- **Treat `NULL` as unknown, never as zero.** Rows written before Task 3's migration have NULL in every new column, permanently.
- **`solar_routes.router` must be included BEFORE the static mount** in `app.py`. The mount at `app.py:240` is a catch-all; anything registered after it is unreachable.
- **Every energy value from Tesla is watt-hours**; every power value is watts. Convert at the boundary, never in the middle.
- **Timezone is `settings.timezone`** (`America/Denver`). All date boundaries are local midnight.
- **API COST IS A HARD CONSTRAINT.** Confirmed rates: data 500/$1, commands 1,000/$1, wakes 50/$1, streaming 150,000/$1, against a $10/month credit — i.e. **5,000 data requests/month, ~167/day for everything.** Every avoidable request is real money. Specifically: never issue a `/vehicles/{vin}` state check when the car is known awake; never fetch `vehicle_data` on a tick that does not need it; treat a wake ($0.02, 20× a data call) as expensive and rate-limited. See spec §1.7.1.
- Python: `from __future__ import annotations` at the top of every new module, matching the existing files.
- Commit messages end with: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `tesla.py` (modify) | Add cross-process refresh locking | 1 |
| `collector.py` (create) | The single background agent: history sampling **and** the solar loop | 2, 9 |
| `com.tenxcious.tesla-collector.plist` (create) | launchd agent | 2 |
| `store.py` (modify) | Schema migration; extend `record()` | 3 |
| `vehicle.py` (modify) | Surface 4 missing fields | 3 |
| `energy.py` (modify) | Emit `grid_export_from_solar` | 3 |
| `home.py` (create) | `home_config` table + `classify()` | 4 |
| `solar.py` (create) | Pure `control()` + `advance()`; config/state/tick persistence | 5, 6, 7 |
| `solar_routes.py` (create) | `/api/car/solar/*` and `/api/car/home` | 8 |
| `static/setup.html`, `static/setup.js` (create) | Home pin, radius, mode config | 10 |
| `static/car.js` (modify) | Solar status card | 11 |
| `demo.py` (modify) | Demo fixtures for the new surfaces | 11 |
| `tools/backtest.py` (create) | Replay real history through the controller | 12 |

**Dependency order:** 1 → 2 → 3 → (4, 5, 6 parallel) → 7 → 8 → 9 → (10, 11) → 12.
Tasks 1 and 2 are prerequisites carried over from the car-page plan and must land first.

---

## Task 1: Cross-process token safety

Tesla's refresh tokens are single-use and rotate on every exchange (`tesla.py:88-92`). `TeslaClient._refresh_lock` is an `asyncio.Lock` — meaningless across processes. Once Task 2 adds a second process, the web app and the collector can refresh simultaneously: one wins, the other presents a spent token, gets 401, and `_refresh` calls `self.store.clear()` — **deleting the grant and forcing an interactive browser re-login.**

**Files:**
- Modify: `tesla.py:86-124` (TokenStore), `tesla.py:200-235` (`_refresh`, `_access_token`), `tesla.py:260-267` and `tesla.py:283-289` (401 retry paths)
- Test: `tests/test_token_lock.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `tesla.TokenStore.reload() -> Tokens | None` — forces a disk read, bypassing the in-memory cache.
  - `tesla.TeslaClient._locked_refresh() -> Tokens` — the only path that may call `_refresh`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_token_lock.py`:

```python
"""Two processes must never both spend the same single-use refresh token."""
from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from tesla import TokenStore, Tokens, _acquire_lock, _release_lock


def _write(path: Path, access: str, expires_at: float) -> None:
    path.write_text(json.dumps(
        {"access_token": access, "refresh_token": "r0", "expires_at": expires_at}))


def test_reload_sees_another_process_write(tmp_path):
    """load() caches; reload() must not."""
    p = tmp_path / ".tokens.json"
    _write(p, "first", time.time() + 9999)
    store = TokenStore(p)
    assert store.load().access_token == "first"

    _write(p, "second", time.time() + 9999)     # simulates the other process
    assert store.load().access_token == "first"    # cached, as designed
    assert store.reload().access_token == "second"  # forced re-read


def _hold(path_str: str, started, release):
    """Child: take the lock, signal, hold until told to let go."""
    fd = _acquire_lock(Path(path_str))
    started.set()
    release.wait(timeout=10)
    _release_lock(fd)


def test_lock_is_exclusive_across_processes(tmp_path):
    p = tmp_path / ".tokens.json"
    _write(p, "first", time.time() + 9999)
    started, release = mp.Event(), mp.Event()
    child = mp.Process(target=_hold, args=(str(p), started, release))
    child.start()
    try:
        assert started.wait(timeout=10), "child never acquired"
        t0 = time.time()
        release.set()
        fd = _acquire_lock(p)          # must block until the child releases
        waited = time.time() - t0
        _release_lock(fd)
        assert waited >= 0.0
    finally:
        release.set()
        child.join(timeout=10)


def test_lock_file_is_a_sidecar_not_the_token_file(tmp_path):
    """save() replaces the token file's inode; an flock on it would be lost."""
    p = tmp_path / ".tokens.json"
    _write(p, "first", time.time() + 9999)
    fd = _acquire_lock(p)
    _release_lock(fd)
    assert (tmp_path / ".tokens.lock").exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_token_lock.py -v`
Expected: FAIL with `ImportError: cannot import name '_acquire_lock' from 'tesla'`

- [ ] **Step 3: Add the lock helpers and `reload()`**

In `tesla.py`, add `import fcntl` to the imports at the top. Then insert immediately **above** `class TokenStore:` (currently line 86):

```python
def _acquire_lock(token_path: Path) -> int:
    """Take an exclusive cross-process lock guarding token refresh.

    Locks a sidecar `.lock` file, never the token file itself: TokenStore.save()
    uses os.replace(), which swaps the inode, and an flock follows the inode —
    so a lock taken on the token file would be silently released mid-write.
    Blocking, so callers on an event loop must acquire via asyncio.to_thread.
    """
    lock_path = token_path.with_suffix(".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_lock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
```

Then add this method to `TokenStore`, immediately after `load()` (which ends at line 107):

```python
    def reload(self) -> Tokens | None:
        """Force a read from disk. Another process may have refreshed since we
        last looked, and its token is the only valid one."""
        self._loaded = False
        return self.load()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_token_lock.py -v`
Expected: 3 passed

- [ ] **Step 5: Route every refresh through the lock**

In `tesla.py`, add this method to `TeslaClient`, immediately after `_refresh` (which ends at line 219):

```python
    async def _locked_refresh(self) -> Tokens:
        """The ONLY path permitted to call _refresh.

        Holds an inter-process lock and re-reads from disk after acquiring it,
        because the process that held the lock before us may have already
        refreshed — in which case its token is valid and ours is spent.
        """
        fd = await asyncio.to_thread(_acquire_lock, self.store.path)
        try:
            tokens = self.store.reload()
            if tokens is None:
                raise TeslaAuthError("Not logged in.")
            if not tokens.expired:
                return tokens          # another process already did the work
            return await self._refresh(tokens)
        finally:
            _release_lock(fd)
```

Replace the body of `_access_token` (lines 221-235) with:

```python
    async def _access_token(self) -> str:
        tokens = self.store.load()
        if tokens is None:
            raise TeslaAuthError("Not logged in.")
        if not tokens.expired:
            return tokens.access_token
        async with self._refresh_lock:            # coroutines in THIS process
            return (await self._locked_refresh()).access_token
```

In `_get`, replace lines 260-267 with:

```python
            if resp.status_code == 401 and attempt == 0:
                # Access token rejected despite not looking expired — force one
                # refresh, retry once.
                async with self._refresh_lock:
                    await self._locked_refresh()
                continue
```

In `_post`, replace lines 283-289 with the identical five lines.

- [ ] **Step 6: Verify no direct `_refresh` callers remain**

Run: `grep -n "_refresh(" tesla.py`
Expected: exactly three lines — the `async def _refresh` definition, the `return await self._refresh(tokens)` inside `_locked_refresh`, and the `_refresh_lock` attribute lines. No other call sites.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all existing tests pass (51 before this task) plus 3 new.

- [ ] **Step 8: Correct the false "FREE" comment**

In `config.py:75`, replace the comment:

```python
    # Adaptive poll intervals, seconds. Asleep uses the cheap, sleep-safe state
    # check only. NOT free: Tesla bills every response with status < 500.
```

- [ ] **Step 9: Add the lock file to .gitignore**

Append to `.gitignore`:

```
.tokens.lock
```

- [ ] **Step 10: Commit**

```bash
git add tesla.py config.py .gitignore tests/test_token_lock.py
git commit -m "fix: guard token refresh with a cross-process lock

Refresh tokens are single-use and rotate. TeslaClient._refresh_lock is an
asyncio.Lock and does not span processes, so a second background agent could
race the web app: the loser presents a spent token, gets 401, and _refresh
calls store.clear() -- deleting the grant and forcing interactive re-login.

Locks a sidecar .lock file rather than the token file, because save() swaps
the inode via os.replace() and an flock follows the inode.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: The collector under launchd

Carried over verbatim from the car-page plan's Task 5, which was designed but never implemented. History accrues only from the moment this runs and cannot be backfilled.

**Files:**
- Create: `collector.py`, `tests/test_collector.py`, `com.tenxcious.tesla-collector.plist`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `tesla.TeslaClient`, `vehicle.derive`, `store.Store`, `config.settings`.
- Produces:
  - `collector.next_interval(car_state: str, view: dict | None, cfg) -> int`
  - `collector.poll_once(client, store, vin, cfg) -> tuple[str, dict | None]`
  - `collector.run(once: bool = False) -> int`

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
  * Every paid vehicle_data call is gated on the cheap, sleep-safe state check,
    because a 408 from a sleeping car is billed like any other request.
  * It never wakes the car. On 2021+ vehicles polling does not prevent sleep --
    only commands do -- so this costs no vampire drain.
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
    # Driving changes SoC fastest, and a drive is short -- check it first so a
    # car that is both moving and (briefly) charging still samples densely.
    if view.get("shift") in DRIVING:
        return cfg.poll_driving
    if view.get("charging"):
        return cfg.poll_charging
    return cfg.poll_idle


async def poll_once(client: TeslaClient, store: Store, vin: str, cfg):
    """One cycle: cheap state check, then a paid read only if it can succeed."""
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

Expected: a line like `2026-07-26T08:20:01 online soc=76%`, or `... offline` if the car is asleep — both are correct outcomes. If it printed a SoC, confirm it landed:

```bash
.venv/bin/python -c "
import sqlite3; d=sqlite3.connect('car.db')
print(d.execute('SELECT ts, battery_level, charging_state FROM samples ORDER BY ts DESC LIMIT 3').fetchall())"
```

- [ ] **Step 6: Lengthen `poll_asleep` to fit the API budget**

`config.py:79` defaults `poll_asleep` to 300 s. At 288 state checks/day that is
**$14.40/month against a $10 credit** — the collector alone would bust the
budget before the solar loop exists.

Change the default to 1800:

```python
    poll_asleep: int = field(default_factory=lambda: int(_clean(os.getenv("POLL_ASLEEP")) or 1800))
```

Also change `poll_idle` from 900 to 1800 on the line above it, for the same
reason. A parked, awake, idle car changes nothing worth sampling every 15
minutes.

Nothing is lost: a sleeping car emits no data, so the only casualty is
precision about *when* it woke. Update `.env.example` if it documents these.

Run `.venv/bin/python -m pytest tests/test_collector.py -v` — the interval
tests pass explicit values via `SimpleNamespace` and must stay green.

- [ ] **Step 7: Write the launchd agent**

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

Append `collector.log` to `.gitignore`.

- [ ] **Step 8: Install and verify the agent is running**

```bash
cp /Users/d/Code/tesla_automation/com.tenxcious.tesla-collector.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.tenxcious.tesla-collector.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.tenxcious.tesla-collector.plist
sleep 5
launchctl list | grep tesla-collector
tail -5 /Users/d/Code/tesla_automation/collector.log
```

Expected: `launchctl list` shows the label with exit status `0` in the second column, and the log shows a `collecting for 5YJSA...` line followed by a state line.

- [ ] **Step 9: Commit**

```bash
git add collector.py tests/test_collector.py com.tenxcious.tesla-collector.plist .gitignore
git commit -m "feat: adaptive SoC collector running under launchd

History accrues only from this moment and cannot be backfilled.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Schema migration, writer, and the missing view-model fields

**A migration alone is inert.** `store.record()` (`store.py:69-80`) writes a hardcoded 15-column `INSERT`; adding columns via `ALTER TABLE` without touching it produces permanently-NULL columns and captures nothing. All three edits ship together or none do.

**Files:**
- Modify: `store.py:19-45` (SCHEMA), `store.py:48-57` (`__init__`), `store.py:62-85` (`record`)
- Modify: `vehicle.py:55-120` (add 4 keys)
- Modify: `energy.py:30-83` (`derive` — emit one key)
- Test: `tests/test_store.py` (extend), `tests/test_vehicle.py` (extend)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `store.Store` persists `charge_energy_added`, `charger_actual_current`, `charger_voltage`, `fast_charger_present`, `fast_charger_type`, `at_home`.
  - `store.Store.record(view, at_home: str | None = None)` — the second parameter is how Task 9 stamps location classification.
  - `vehicle.derive()` additionally emits `volts`, `fast_charger_present`, `homelink_nearby`, `homelink_devices`.
  - `energy.derive()` additionally emits `grid_export_from_solar`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py`:

```python
def test_migration_adds_columns_to_an_existing_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS will not alter an existing table."""
    import sqlite3
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE samples (
          ts INTEGER NOT NULL, vin TEXT NOT NULL, battery_level INTEGER,
          usable_battery_level INTEGER, charge_limit_soc INTEGER,
          charging_state TEXT, charging INTEGER, charger_power INTEGER,
          range_mi REAL, odometer REAL, inside_temp REAL, outside_temp REAL,
          latitude REAL, longitude REAL, shift_state TEXT,
          PRIMARY KEY (vin, ts));
    """)
    legacy.commit()
    legacy.close()

    from store import Store
    s = Store(path)
    cols = {r[1] for r in s._db.execute("PRAGMA table_info(samples)")}
    for name in ("charge_energy_added", "charger_actual_current", "charger_voltage",
                 "fast_charger_present", "fast_charger_type", "at_home"):
        assert name in cols, f"{name} missing after migration"
    s.close()


def test_record_persists_the_new_columns(tmp_path):
    from store import Store
    s = Store(tmp_path / "n.db")
    s.record({
        "vin": "V1", "sampled_at": 1000, "soc": 50, "usable_soc": 50, "limit": 80,
        "charging_state": "Charging", "charging": True, "charge_power_kw": 7,
        "range_mi": 200.0, "odometer_mi": 1.0, "inside_c": 20.0, "outside_c": 10.0,
        "lat": 40.0, "lon": -105.0, "shift": "P",
        "energy_added_kwh": 12.5, "amps_actual": 24, "volts": 240,
        "fast_charger_present": False, "fast_charger": "SNA",
    }, at_home="home")
    row = s._db.execute(
        "SELECT charge_energy_added, charger_actual_current, charger_voltage,"
        " fast_charger_present, fast_charger_type, at_home FROM samples").fetchone()
    assert row["charge_energy_added"] == 12.5
    assert row["charger_actual_current"] == 24
    assert row["charger_voltage"] == 240
    assert row["fast_charger_present"] == 0
    assert row["fast_charger_type"] == "SNA"
    assert row["at_home"] == "home"
    s.close()


def test_record_tolerates_a_view_missing_the_new_keys(tmp_path):
    """Pre-existing callers pass views without them; NULL means unknown."""
    from store import Store
    s = Store(tmp_path / "m.db")
    s.record({"vin": "V1", "sampled_at": 1, "soc": 50})
    row = s._db.execute("SELECT charge_energy_added, at_home FROM samples").fetchone()
    assert row["charge_energy_added"] is None
    assert row["at_home"] is None
    s.close()
```

Append to `tests/test_vehicle.py`:

```python
def test_derive_surfaces_the_fields_the_solar_loop_needs():
    import json
    from pathlib import Path
    import vehicle
    raw = json.loads((Path(__file__).parent / "fixtures" / "vehicle_data.json").read_text())
    view = vehicle.derive(raw)
    for key in ("volts", "fast_charger_present", "homelink_nearby", "homelink_devices"):
        assert key in view, f"{key} missing from derive()"
    assert view["homelink_devices"] == 2


def test_volts_is_none_when_idle_because_the_sensor_reads_two():
    import vehicle
    view = vehicle.derive({"response": {
        "charge_state": {"charging_state": "Disconnected", "charger_voltage": 2},
        "climate_state": {}, "drive_state": {}, "vehicle_state": {},
        "gui_settings": {}, "vehicle_config": {}}})
    assert view["volts"] is None


def test_volts_is_reported_while_charging():
    import vehicle
    view = vehicle.derive({"response": {
        "charge_state": {"charging_state": "Charging", "charger_voltage": 241},
        "climate_state": {}, "drive_state": {}, "vehicle_state": {},
        "gui_settings": {}, "vehicle_config": {}}})
    assert view["volts"] == 241
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_store.py tests/test_vehicle.py -v`
Expected: the five new tests FAIL — `KeyError` on the new view keys and missing columns.

- [ ] **Step 3: Add the migration to `store.py`**

Extend `SCHEMA` (line 19) so a *fresh* database gets the columns directly — add these six lines inside the `samples` table definition, immediately after `shift_state TEXT,`:

```sql
  charge_energy_added    REAL,
  charger_actual_current INTEGER,
  charger_voltage        INTEGER,
  fast_charger_present   INTEGER,
  fast_charger_type      TEXT,
  at_home                TEXT,
```

Add this module-level function immediately above `class Store:` (line 48):

```python
NEW_COLUMNS = (
    ("charge_energy_added", "REAL"),
    ("charger_actual_current", "INTEGER"),
    ("charger_voltage", "INTEGER"),
    ("fast_charger_present", "INTEGER"),
    ("fast_charger_type", "TEXT"),
    ("at_home", "TEXT"),
)


def _migrate(db: sqlite3.Connection) -> None:
    """Add columns to an EXISTING samples table.

    `CREATE TABLE IF NOT EXISTS` in SCHEMA is a no-op against a table that
    already exists, so a schema edit alone never reaches a live car.db.
    Rows written before this runs keep NULL in every new column forever --
    consumers must read NULL as "unknown", never as zero.
    """
    existing = {row[1] for row in db.execute("PRAGMA table_info(samples)")}
    for name, decl in NEW_COLUMNS:
        if name not in existing:
            db.execute(f"ALTER TABLE samples ADD COLUMN {name} {decl}")
```

In `Store.__init__`, call it after `executescript` (line 56):

```python
        self._db.executescript(SCHEMA)
        _migrate(self._db)
        self._db.commit()
```

- [ ] **Step 4: Extend `record()` to write them**

Replace the `INSERT` statement in `record()` (lines 69-80) with:

```python
        self._db.execute(
            """INSERT OR REPLACE INTO samples
               (ts, vin, battery_level, usable_battery_level, charge_limit_soc,
                charging_state, charging, charger_power, range_mi, odometer,
                inside_temp, outside_temp, latitude, longitude, shift_state,
                charge_energy_added, charger_actual_current, charger_voltage,
                fast_charger_present, fast_charger_type, at_home)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, vin, view.get("soc"), view.get("usable_soc"), view.get("limit"),
             view.get("charging_state"), int(bool(view.get("charging"))),
             view.get("charge_power_kw"), view.get("range_mi"),
             view.get("odometer_mi"), view.get("inside_c"), view.get("outside_c"),
             view.get("lat"), view.get("lon"), view.get("shift"),
             view.get("energy_added_kwh"), view.get("amps_actual"),
             view.get("volts"),
             None if view.get("fast_charger_present") is None
                  else int(bool(view.get("fast_charger_present"))),
             view.get("fast_charger"), at_home),
        )
```

And change the signature (line 62):

```python
    def record(self, view: dict[str, Any], at_home: str | None = None) -> None:
```

- [ ] **Step 5: Add the four fields to `vehicle.derive()`**

In `vehicle.py`, insert after the `"fast_charger"` line (line 78):

```python
        "fast_charger_present": charge.get("fast_charger_present"),
        # charger_voltage reads 2, not 0, when idle -- so it is only meaningful
        # mid-session. The solar controller converts amps to watts with it.
        "volts": (charge.get("charger_voltage")
                  if charging_state in CHARGING_STATES else None),
```

And in the `vehicle_state` section, alongside the other body fields:

```python
        "homelink_nearby": state.get("homelink_nearby"),
        "homelink_devices": state.get("homelink_device_count"),
```

- [ ] **Step 6: Emit `grid_export_from_solar` from `energy.derive()`**

`energy.py:63` already computes this value inside the `self_consumption` expression and throws it away. Hoist it. Above the `self_sufficiency` line (line 60), add:

```python
    grid_export_from_solar = _wh(row, "grid_energy_exported_from_solar")
```

Change the `self_consumption` expression (lines 62-64) to reuse it:

```python
    self_consumption = (
        ((solar - grid_export_from_solar) / solar * 100) if solar > 0 else None
    )
```

And add to the returned dict, after `"grid_export"`:

```python
        "grid_export_from_solar": kwh(grid_export_from_solar),
```

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. The five new tests plus everything prior.

- [ ] **Step 8: Verify the migration against the real database**

```bash
cp car.db /tmp/car.db.premigration
.venv/bin/python -c "
from pathlib import Path
from store import Store
s = Store(Path('car.db'))
cols = [r[1] for r in s._db.execute('PRAGMA table_info(samples)')]
print('columns:', cols)
n = s._db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
print('rows preserved:', n)
s.close()"
```

Expected: all six new column names present, and the row count matches what was there before (no data loss).

- [ ] **Step 9: Commit**

```bash
git add store.py vehicle.py energy.py tests/test_store.py tests/test_vehicle.py
git commit -m "feat: persist charge energy, amps, volts, and location class

A schema edit alone is inert: record() writes a hardcoded column list, so
ALTER without touching the writer yields permanently-NULL columns. Migration,
writer, and view model land together.

charge_energy_added cannot be backfilled, so capture starts now even though
its consumer (the green-energy graphs) is a later spec.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `home.py` — home location and three-valued classification

**Files:**
- Create: `home.py`, `tests/test_home.py`

**Interfaces:**
- Consumes: `store.Store` (for the connection).
- Produces:
  - `home.HomeConfig` — dataclass `(latitude: float, longitude: float, radius_m: int)`
  - `home.SCHEMA: str` — the `home_config` DDL, executed by `Store`
  - `home.load(db) -> HomeConfig | None`
  - `home.save(db, latitude, longitude, radius_m) -> None`
  - `home.distance_m(lat1, lon1, lat2, lon2) -> float`
  - `home.classify(view: dict, cfg: HomeConfig | None) -> str` returning `"home" | "away" | "unknown"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_home.py`:

```python
from __future__ import annotations

import sqlite3

import pytest

import home

DENVER = (40.1672, -105.1019)          # near Longmont
CFG = home.HomeConfig(latitude=DENVER[0], longitude=DENVER[1], radius_m=100)


def _view(**kw):
    base = {"lat": DENVER[0], "lon": DENVER[1],
            "fast_charger_present": False, "fast_charger": "SNA"}
    base.update(kw)
    return base


def test_distance_is_zero_at_the_same_point():
    assert home.distance_m(*DENVER, *DENVER) == pytest.approx(0.0, abs=0.5)


def test_distance_matches_a_known_offset():
    # 0.001 degrees of latitude is ~111 m anywhere on earth.
    d = home.distance_m(DENVER[0], DENVER[1], DENVER[0] + 0.001, DENVER[1])
    assert d == pytest.approx(111.0, abs=3.0)


def test_inside_the_radius_is_home():
    assert home.classify(_view(), CFG) == "home"


def test_outside_the_radius_is_away():
    assert home.classify(_view(lat=DENVER[0] + 0.01), CFG) == "away"


def test_absent_coordinates_are_unknown_not_away():
    """Tesla OMITS location keys rather than nulling them. Three facts collapse
    into one value, and defaulting to home is how a Supercharger session
    pollutes the numbers."""
    assert home.classify(_view(lat=None, lon=None), CFG) == "unknown"
    v = _view()
    del v["lat"]
    assert home.classify(v, CFG) == "unknown"


def test_no_home_configured_is_unknown():
    assert home.classify(_view(), None) == "unknown"


def test_a_dc_fast_charger_is_away_even_at_home():
    """Geofence radius cannot rule out a Supercharger parked on the driveway
    coordinate; the charger type must."""
    assert home.classify(_view(fast_charger_present=True), CFG) == "away"
    assert home.classify(_view(fast_charger="Supercharger"), CFG) == "away"
    assert home.classify(_view(fast_charger="Combo"), CFG) == "away"
    assert home.classify(_view(fast_charger="Chademo"), CFG) == "away"
    assert home.classify(_view(fast_charger="Gb"), CFG) == "away"


def test_an_ac_charger_at_home_stays_home():
    assert home.classify(_view(fast_charger="SNA"), CFG) == "home"
    assert home.classify(_view(fast_charger=None), CFG) == "home"


def test_save_and_load_round_trip():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(home.SCHEMA)
    assert home.load(db) is None
    home.save(db, 40.5, -105.5, 150)
    cfg = home.load(db)
    assert (cfg.latitude, cfg.longitude, cfg.radius_m) == (40.5, -105.5, 150)
    home.save(db, 41.0, -106.0, 75)      # single row, overwritten
    assert db.execute("SELECT COUNT(*) FROM home_config").fetchone()[0] == 1
    assert home.load(db).radius_m == 75
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_home.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'home'`

- [ ] **Step 3: Write `home.py`**

```python
"""Where the car lives, and whether it is there right now.

Nothing in the Fleet API exposes the car's own saved Home address -- no
navigation favourites endpoint, no Home/Work field in vehicle_data. So home is
a pin the owner drops, stored here.

The answer is deliberately three-valued. Tesla OMITS location keys rather than
nulling them when the scope is missing, so "scope revoked", "location sharing
off" and "not home" all arrive as the same absence. Collapsing them into a
boolean is how a Supercharger session silently pollutes home-only accounting.
"""
from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS home_config (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  latitude   REAL    NOT NULL,
  longitude  REAL    NOT NULL,
  radius_m   INTEGER NOT NULL DEFAULT 100,
  updated_at INTEGER NOT NULL
);
"""

# A DC fast charger proves the car is not on home AC, whatever the coordinates
# say. Values per docs/tesla-field-reference.md:118.
DC_CHARGER_TYPES = {"Supercharger", "Combo", "Chademo", "Gb"}

EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class HomeConfig:
    latitude: float
    longitude: float
    radius_m: int


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine).

    Accurate to ~0.5% -- far tighter than GPS multipath in a garage, which is
    the actual error budget here.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def classify(view: dict, cfg: HomeConfig | None) -> str:
    """"home" | "away" | "unknown". Never a boolean -- see the module docstring."""
    if cfg is None:
        return "unknown"
    lat, lon = view.get("lat"), view.get("lon")
    if lat is None or lon is None:
        return "unknown"
    if view.get("fast_charger_present"):
        return "away"
    if view.get("fast_charger") in DC_CHARGER_TYPES:
        return "away"
    return "home" if distance_m(lat, lon, cfg.latitude, cfg.longitude) <= cfg.radius_m else "away"


def load(db: sqlite3.Connection) -> HomeConfig | None:
    row = db.execute(
        "SELECT latitude, longitude, radius_m FROM home_config WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return HomeConfig(latitude=row["latitude"], longitude=row["longitude"],
                      radius_m=row["radius_m"])


def save(db: sqlite3.Connection, latitude: float, longitude: float,
         radius_m: int) -> None:
    db.execute(
        """INSERT INTO home_config (id, latitude, longitude, radius_m, updated_at)
           VALUES (1, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             latitude = excluded.latitude, longitude = excluded.longitude,
             radius_m = excluded.radius_m, updated_at = excluded.updated_at""",
        (latitude, longitude, radius_m, int(time.time())),
    )
    db.commit()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_home.py -v`
Expected: 10 passed

- [ ] **Step 5: Register the schema with `Store`**

In `store.py`, import at the top:

```python
import home
```

and in `Store.__init__`, after `_migrate(self._db)`:

```python
        self._db.executescript(home.SCHEMA)
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add home.py tests/test_home.py store.py
git commit -m "feat: home geofence with three-valued classification

Tesla omits location keys rather than nulling them, so unknown is a distinct
state from away and must not default to home.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `solar.py` — the pure control law

Read spec §3.1-3.2 first. The symbols are deliberately disjoint: `error_w` is a signed control error driven to zero; `surplus_w` is an absolute quantity. An earlier draft conflated them and the controller dropped into `grace` on every tick of successful charging.

**Files:**
- Create: `solar.py`, `tests/test_solar_control.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `solar.Tunables` — dataclass `(margin_w, deadband_w, ramp_a, min_a, max_a, volts)`
  - `solar.Decision` — dataclass `(target_a: int, write: bool, floor_breach: bool, error_w: float, raw_target: float)`
  - `solar.car_watts(view: dict, volts: int) -> float`
  - `solar.surplus_watts(car_w: float, grid_w: float) -> float`
  - `solar.control(grid_w: float, current_a: int, tun: Tunables) -> Decision`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_solar_control.py`:

```python
from __future__ import annotations

import pytest

import solar

T = solar.Tunables(margin_w=100, deadband_w=250, ramp_a=8, min_a=5, max_a=48, volts=240)


def test_car_watts_is_zero_unless_actually_charging():
    """In idle and stopped the car draws nothing. Using the standing amps
    setting instead would invent 1.2-11.5 kW of surplus that does not exist."""
    assert solar.car_watts({"charging_state": "Stopped", "amps_actual": 32}, 240) == 0
    assert solar.car_watts({"charging_state": "Disconnected", "amps_actual": 32}, 240) == 0
    assert solar.car_watts({"charging_state": "Complete", "amps_actual": 0}, 240) == 0
    assert solar.car_watts({"charging_state": "Charging", "amps_actual": 20}, 240) == 4800
    assert solar.car_watts({"charging_state": "Starting", "amps_actual": 5}, 240) == 1200


def test_car_watts_is_zero_when_amps_unknown():
    assert solar.car_watts({"charging_state": "Charging", "amps_actual": None}, 240) == 0


def test_surplus_is_absolute_and_includes_the_cars_own_draw():
    # Car pulling 9.6 kW with the meter at zero: all 9.6 kW is solar.
    assert solar.surplus_watts(9600, 0) == 9600
    # Car off, exporting 3 kW: 3 kW is available.
    assert solar.surplus_watts(0, -3000) == 3000
    # Car pulling 1.2 kW while importing 400 W: only 800 W is solar.
    assert solar.surplus_watts(1200, 400) == 800


@pytest.mark.parametrize("name,grid_w,current_a,expect_target,expect_breach", [
    # The converged states MUST NOT breach. This is the regression that a
    # previous design failed: a perfectly charging car read as below floor.
    ("converged 3kW sun at 12A",      -120, 12, 12, False),
    ("converged 9.6kW sun at 40A",       0, 40, 40, False),
    ("sun rising, headroom at 12A",  -2000, 12, 20, False),
    ("AC starts, must back off",      1500, 20, 13, False),
    ("cloud, genuine floor breach",    400,  5,  5, True),
    ("ramp limit caps the climb",    -5000, 10, 18, False),
    ("clamps to max",               -20000, 45, 48, False),
])
def test_control_law(name, grid_w, current_a, expect_target, expect_breach):
    d = solar.control(grid_w, current_a, T)
    assert d.target_a == expect_target, name
    assert d.floor_breach is expect_breach, name


def test_deadband_suppresses_the_write():
    d = solar.control(-100, 20, T)          # error_w = 0
    assert d.write is False
    assert d.target_a == 20


def test_outside_the_deadband_writes():
    d = solar.control(-500, 20, T)          # error_w = 400 > 250
    assert d.write is True
    assert d.target_a == 22


def test_target_is_always_an_integer():
    d = solar.control(-333, 11, T)
    assert isinstance(d.target_a, int)


def test_raw_target_is_unclamped_so_the_floor_test_can_see_below_min():
    d = solar.control(400, 5, T)            # importing 400 W at the floor
    assert d.raw_target < T.min_a
    assert d.target_a == T.min_a            # but we never COMMAND below min
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_solar_control.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'solar'`

- [ ] **Step 3: Write the control law**

Create `solar.py`:

```python
"""Closed-loop solar charge control.

Everything here is pure: it takes numbers and returns numbers. collector.py
owns all I/O. That split is what makes a control system testable without a car,
a roof, or the weather.

THE CENTRAL IDEA. grid_power already contains the car's own draw, so the loop
never needs to know what the car is consuming in absolute terms. It servos the
meter: push amps up while the site exports, back off while it imports, and the
system converges on grid ~= -margin_w. House disturbances (an AC compressor
starting) are rejected as a matter of course.

Computing surplus as `solar_power - load_power` instead would create positive
feedback -- raise amps, load rises, apparent surplus collapses, controller
backs off, oscillate. And it is not available anyway: live_status.wall_connectors
is empty on this site, so there is no site-side measurement of the car.

TWO DISTINCT QUANTITIES, never conflated:
  error_w   -- signed control error, driven to ZERO. Used only by control().
  surplus_w -- absolute solar available to the car. Used by the state machine,
               the UI, and logging.
A converged loop holds error_w near zero while surplus_w may be 9600 W.
Comparing error_w against an absolute floor makes a healthy charge look like a
dead one.
"""
from __future__ import annotations

from dataclasses import dataclass

# charger_voltage reads 2 (not 0) when idle, so power is only meaningful in
# these two states -- docs/tesla-field-reference.md:97.
LIVE_CHARGING_STATES = {"Charging", "Starting"}


@dataclass(frozen=True)
class Tunables:
    margin_w: int = 100      # bias toward exporting a trickle rather than importing
    deadband_w: int = 250    # ~= one amp step; the smallest that cannot oscillate
    ramp_a: int = 8          # max amps of change per tick
    min_a: int = 5           # the car's own UI floor; below this needs a double-send
    max_a: int = 48          # charge_current_request_max
    volts: int = 240


@dataclass(frozen=True)
class Decision:
    target_a: int            # what to command, clamped and integral
    write: bool              # False when inside the deadband
    floor_breach: bool       # the law wanted less than min_a
    error_w: float
    raw_target: float        # unclamped; exists only to answer floor_breach


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def car_watts(view: dict, volts: int) -> float:
    """The car's present AC draw, or 0.

    Pinned to ACTUAL charging, never to the standing amps setting: in idle and
    stopped the car draws nothing, and adding a nonexistent 1.2-11.5 kW would
    inflate surplus and start a charge into surplus that is not there.
    """
    if view.get("charging_state") not in LIVE_CHARGING_STATES:
        return 0.0
    amps = view.get("amps_actual")
    if amps is None:
        return 0.0
    return float(amps) * volts


def surplus_watts(car_w: float, grid_w: float) -> float:
    """Absolute solar available to the car. grid_w > 0 is import."""
    return car_w - grid_w


def control(grid_w: float, current_a: int, tun: Tunables) -> Decision:
    """One tick of the integral controller."""
    error_w = -grid_w - tun.margin_w
    raw_target = current_a + error_w / tun.volts
    step_a = int(round(_clamp(error_w / tun.volts, -tun.ramp_a, tun.ramp_a)))
    target_a = int(_clamp(current_a + step_a, tun.min_a, tun.max_a))
    return Decision(
        target_a=target_a,
        write=abs(error_w) >= tun.deadband_w,
        # Expressed in AMPS, not watts, so the floor derives from the measured
        # voltage instead of a hardcoded 1200 W -- and so it stays correct in
        # every state rather than only when the car is idle.
        floor_breach=raw_target < tun.min_a,
        error_w=error_w,
        raw_target=raw_target,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_solar_control.py -v`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add solar.py tests/test_solar_control.py
git commit -m "feat: pure incremental control law for solar charging

Servos the grid meter rather than computing surplus from solar minus load,
which would create positive feedback because grid_power already contains the
car's draw.

error_w (converges to zero) and surplus_w (absolute) are kept strictly
separate; the floor test is expressed in amps so it holds in every state.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `solar.py` — the pure state machine

Read spec §3.3. Note the two-tick dwell and the hysteresis band — both exist because `grid_power` refreshes at 60 s while the car's own current comes from a different clock.

**Files:**
- Modify: `solar.py` (append)
- Test: `tests/test_solar_state.py` (create)

**Interfaces:**
- Consumes: `solar.Decision`, `solar.Tunables` from Task 5.
- Produces:
  - `solar.Policy` — dataclass `(grace_s, restart_hold_s, start_hold_s, enabled)`
  - `solar.Machine` — dataclass `(state: str, breach_ticks: int, recover_ticks: int, grace_s_elapsed: int, hold_s: int)`
  - `solar.Tick` — dataclass `(surplus_w, decision, location, plugged, period_s)`
  - `solar.advance(m: Machine, t: Tick, pol: Policy, tun: Tunables) -> tuple[Machine, list[str]]` — returns the next machine plus an ordered list of actions from `{"set_amps", "charge_stop", "charge_start", "restore", "wake"}`
  - `solar.STATES` — the frozenset of legal state names

- [ ] **Step 1: Write the failing tests**

Create `tests/test_solar_state.py`:

```python
from __future__ import annotations

import solar

TUN = solar.Tunables()
POL = solar.Policy(grace_s=180, restart_hold_s=300, start_hold_s=60, enabled=True)
PERIOD = 120
START_W = TUN.min_a * TUN.volts + TUN.margin_w        # 1300


def tick(surplus_w, *, grid_w=None, current_a=5, location="home", plugged=True):
    if grid_w is None:
        grid_w = -surplus_w        # car off: surplus is pure export
    return solar.Tick(
        surplus_w=surplus_w,
        decision=solar.control(grid_w, current_a, TUN),
        location=location, plugged=plugged, period_s=PERIOD,
    )


def m(state, **kw):
    return solar.Machine(state=state, breach_ticks=kw.get("breach_ticks", 0),
                         recover_ticks=kw.get("recover_ticks", 0),
                         grace_s_elapsed=kw.get("grace_s_elapsed", 0),
                         hold_s=kw.get("hold_s", 0))


def test_idle_requires_sustained_surplus_before_charging():
    machine = m("idle")
    machine, actions = solar.advance(machine, tick(3000), POL, TUN)
    assert machine.state == "idle", "must hold for start_hold_s first"
    machine, actions = solar.advance(machine, tick(3000), POL, TUN)
    assert machine.state == "charging"
    assert "charge_start" in actions


def test_idle_ignores_surplus_below_the_floor():
    machine = m("idle", hold_s=999)
    machine, _ = solar.advance(machine, tick(START_W - 1), POL, TUN)
    assert machine.state == "idle"


def test_idle_does_nothing_when_not_plugged_in():
    machine = m("idle", hold_s=999)
    machine, actions = solar.advance(machine, tick(5000, plugged=False), POL, TUN)
    assert machine.state == "idle"
    assert actions == []


def test_charging_converged_does_not_fall_into_grace():
    """THE REGRESSION. A car charging perfectly at 40 A on 9.6 kW of sun has an
    error near zero. An earlier design compared that against a 1200 W floor and
    dropped to grace on every tick, stop/starting all afternoon."""
    machine = m("charging")
    for _ in range(10):
        machine, actions = solar.advance(
            machine, tick(9600, grid_w=0, current_a=40), POL, TUN)
        assert machine.state == "charging"
        assert "charge_stop" not in actions


def test_a_single_breach_tick_does_not_enter_grace():
    """grid_power refreshes at 60 s while the car's current comes from its own
    clock, so one tick after an amps write the two disagree."""
    machine = m("charging")
    machine, _ = solar.advance(machine, tick(800, grid_w=400, current_a=5), POL, TUN)
    assert machine.state == "charging"
    assert machine.breach_ticks == 1


def test_two_consecutive_breach_ticks_enter_grace():
    machine = m("charging", breach_ticks=1)
    machine, actions = solar.advance(machine, tick(800, grid_w=400, current_a=5), POL, TUN)
    assert machine.state == "grace"
    assert "set_amps" in actions          # snap straight to min_a


def test_grace_expiry_stops_and_restores():
    machine = m("grace", grace_s_elapsed=POL.grace_s)
    machine, actions = solar.advance(machine, tick(500, grid_w=700, current_a=5), POL, TUN)
    assert machine.state == "stopped"
    assert actions.index("charge_stop") < actions.index("restore")


def test_grace_recovery_needs_two_ticks_above_a_hysteresis_band():
    machine = m("grace", grace_s_elapsed=60)
    machine, _ = solar.advance(machine, tick(4000, grid_w=-2800, current_a=5), POL, TUN)
    assert machine.state == "grace", "one good tick is not enough"
    machine, _ = solar.advance(machine, tick(4000, grid_w=-2800, current_a=5), POL, TUN)
    assert machine.state == "charging"
    assert machine.grace_s_elapsed == 0, "timer must reset on recovery"


def test_stopped_requires_a_long_hold_before_spending_a_wake():
    machine = m("stopped")
    machine, actions = solar.advance(machine, tick(5000), POL, TUN)
    assert machine.state == "stopped"
    assert "wake" not in actions
    machine = m("stopped", hold_s=POL.restart_hold_s)
    machine, actions = solar.advance(machine, tick(5000), POL, TUN)
    assert machine.state == "charging"
    assert actions.index("wake") < actions.index("charge_start")


def test_stopped_hold_resets_when_surplus_drops():
    machine = m("stopped", hold_s=240)
    machine, _ = solar.advance(machine, tick(200), POL, TUN)
    assert machine.hold_s == 0


def test_unplugging_restores_and_idles_from_any_state():
    for state in ("charging", "grace", "stopped"):
        machine, actions = solar.advance(m(state), tick(3000, plugged=False), POL, TUN)
        assert machine.state == "idle", state
        assert "restore" in actions, state


def test_driving_away_restores_and_idles():
    machine, actions = solar.advance(m("charging"), tick(3000, location="away"), POL, TUN)
    assert machine.state == "idle"
    assert "restore" in actions


def test_unknown_location_freezes_and_issues_nothing():
    """Restoring is itself a command. Not knowing where the car is is not
    grounds to send one."""
    for state in ("charging", "grace", "stopped"):
        machine, actions = solar.advance(m(state), tick(3000, location="unknown"), POL, TUN)
        assert machine.state == state, state
        assert actions == [], state


def test_disabling_restores_and_idles():
    off = solar.Policy(grace_s=180, restart_hold_s=300, start_hold_s=60, enabled=False)
    machine, actions = solar.advance(m("charging"), tick(3000), off, TUN)
    assert machine.state == "idle"
    assert "restore" in actions


def test_every_state_is_declared():
    assert solar.STATES == {"idle", "charging", "grace", "stopped"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_solar_state.py -v`
Expected: FAIL with `AttributeError: module 'solar' has no attribute 'Policy'`

- [ ] **Step 3: Append the state machine to `solar.py`**

```python
STATES = frozenset({"idle", "charging", "grace", "stopped"})


@dataclass(frozen=True)
class Policy:
    grace_s: int = 180          # hold at min_a this long before giving up
    restart_hold_s: int = 300   # sustained surplus before spending a wake
    start_hold_s: int = 60      # sustained surplus before starting from idle
    enabled: bool = True


@dataclass(frozen=True)
class Machine:
    state: str = "idle"
    breach_ticks: int = 0       # consecutive ticks below the floor
    recover_ticks: int = 0      # consecutive ticks back above it
    grace_s_elapsed: int = 0
    hold_s: int = 0             # sustained-surplus timer for idle and stopped


@dataclass(frozen=True)
class Tick:
    surplus_w: float
    decision: Decision
    location: str               # "home" | "away" | "unknown"
    plugged: bool
    period_s: int


def start_watts(tun: Tunables) -> float:
    """The absolute surplus needed to sustain the minimum charge rate."""
    return tun.min_a * tun.volts + tun.margin_w


def advance(m: Machine, t: Tick, pol: Policy, tun: Tunables) -> tuple[Machine, list[str]]:
    """One state transition. Returns the next machine and an ORDERED action list.

    Actions are names, not calls -- collector.py performs them. That keeps this
    function pure and lets the backtest run the whole machine with no network.
    """
    # Unknown location freezes everything. Restoring is itself a command, and
    # "we do not know where the car is" is not grounds to send one.
    if t.location == "unknown":
        return m, []

    if not pol.enabled or not t.plugged or t.location != "home":
        if m.state == "idle":
            return Machine(state="idle"), []
        return Machine(state="idle"), ["restore"]

    if m.state == "idle":
        hold = m.hold_s + t.period_s if t.surplus_w >= start_watts(tun) else 0
        if hold >= pol.start_hold_s:
            return Machine(state="charging"), ["charge_start", "set_amps"]
        return Machine(state="idle", hold_s=hold), []

    if m.state == "charging":
        if t.decision.floor_breach:
            breach = m.breach_ticks + 1
            if breach >= 2:      # dwell: one tick can be clock skew, not weather
                return Machine(state="grace"), ["set_amps"]
            return Machine(state="charging", breach_ticks=breach), []
        actions = ["set_amps"] if t.decision.write else []
        return Machine(state="charging"), actions

    if m.state == "grace":
        # Recovery needs a band above the re-entry point, or a surplus sitting
        # exactly at the floor chatters grace<->charging every tick.
        if t.decision.raw_target >= tun.min_a + 1:
            recover = m.recover_ticks + 1
            if recover >= 2:
                return Machine(state="charging"), ["set_amps"]
            return Machine(state="grace", grace_s_elapsed=m.grace_s_elapsed,
                           recover_ticks=recover), []
        elapsed = m.grace_s_elapsed + t.period_s
        if elapsed > pol.grace_s:
            return Machine(state="stopped"), ["charge_stop", "restore"]
        return Machine(state="grace", grace_s_elapsed=elapsed), []

    # stopped
    hold = m.hold_s + t.period_s if t.surplus_w >= start_watts(tun) else 0
    if hold >= pol.restart_hold_s:
        return Machine(state="charging"), ["wake", "charge_start", "set_amps"]
    return Machine(state="stopped", hold_s=hold), []
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_solar_state.py -v`
Expected: 15 passed

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add solar.py tests/test_solar_state.py
git commit -m "feat: solar controller state machine

Two-tick dwell before grace (grid_power refreshes at 60s while the car's own
current comes from a different clock, so one tick can be skew rather than
weather) and a hysteresis band on recovery.

Unknown location freezes rather than restoring: restoring is a command, and
not knowing where the car is is not grounds to send one.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Solar persistence — config, state, ticks, and the request cap

**Files:**
- Modify: `solar.py` (append), `store.py` (register schema)
- Test: `tests/test_solar_store.py` (create)

**Interfaces:**
- Consumes: `solar.Tunables`, `solar.Policy`.
- Produces:
  - `solar.SCHEMA: str`
  - `solar.load_config(db) -> dict` — every column, with defaults applied on first read
  - `solar.save_config(db, **fields) -> None` — partial update
  - `solar.tunables_from(cfg: dict, amps_max: int | None, volts: int | None) -> Tunables`
  - `solar.policy_from(cfg: dict) -> Policy`
  - `solar.load_state(db, vin) -> dict`
  - `solar.save_state(db, vin, **fields) -> None`
  - `solar.log_tick(db, vin, **fields) -> None`
  - `solar.count_request(db, vin, today: str) -> tuple[int, bool]` — returns `(count, capped)`
  - `solar.grace_import_wh(db, vin, since_ts: int) -> float`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_solar_store.py`:

```python
from __future__ import annotations

import sqlite3

import solar


def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(solar.SCHEMA)
    return conn


def test_config_defaults_exist_before_anything_is_written():
    cfg = solar.load_config(db())
    assert cfg["enabled"] == 0
    assert cfg["period_s"] == 120
    assert cfg["soc_ceiling"] == 90
    assert cfg["min_a"] == 5
    assert cfg["daily_request_cap"] == 400


def test_config_partial_update_leaves_other_fields_alone():
    conn = db()
    solar.save_config(conn, enabled=1, soc_ceiling=100)
    cfg = solar.load_config(conn)
    assert cfg["enabled"] == 1
    assert cfg["soc_ceiling"] == 100
    assert cfg["period_s"] == 120, "untouched field must keep its default"


def test_tunables_prefer_the_cars_reported_ceiling_and_voltage():
    cfg = solar.load_config(db())
    t = solar.tunables_from(cfg, amps_max=32, volts=241)
    assert t.max_a == 32
    assert t.volts == 241


def test_tunables_fall_back_when_the_car_reports_nothing():
    """charger_voltage reads 2 when idle, so volts is None until a session
    starts. 240 is the documented nominal for this site."""
    cfg = solar.load_config(db())
    t = solar.tunables_from(cfg, amps_max=None, volts=None)
    assert t.max_a == 48
    assert t.volts == 240


def test_state_round_trips_and_defaults_clean():
    conn = db()
    st = solar.load_state(conn, "V1")
    assert st["dirty"] == 0
    assert st["state"] == "idle"
    solar.save_state(conn, "V1", dirty=1, original_amps=32, original_limit=80)
    st = solar.load_state(conn, "V1")
    assert (st["dirty"], st["original_amps"], st["original_limit"]) == (1, 32, 80)


def test_request_cap_counts_and_trips():
    conn = db()
    solar.save_config(conn, daily_request_cap=3)
    for expected in (1, 2, 3):
        count, capped = solar.count_request(conn, "V1", "2026-07-26")
        assert count == expected
    assert capped is True
    count, capped = solar.count_request(conn, "V1", "2026-07-26")
    assert capped is True


def test_request_counter_resets_on_a_new_local_day():
    conn = db()
    solar.save_config(conn, daily_request_cap=3)
    for _ in range(3):
        solar.count_request(conn, "V1", "2026-07-26")
    count, capped = solar.count_request(conn, "V1", "2026-07-27")
    assert count == 1
    assert capped is False


def test_the_whole_machine_survives_a_round_trip():
    """Each tick reloads from the database. A counter that does not persist
    resets every tick, and the two-tick dwell can never reach two."""
    conn = db()
    m = solar.Machine(state="grace", breach_ticks=2, recover_ticks=1,
                      grace_s_elapsed=120, hold_s=60)
    solar.save_state(conn, "V1", **solar.machine_fields(m))
    assert solar.machine_from(solar.load_state(conn, "V1")) == m


def test_machine_fields_covers_every_machine_attribute():
    """Guards against someone adding a counter to Machine and forgetting to
    persist it -- which would silently disable a hysteresis path."""
    import dataclasses
    assert set(solar.MACHINE_FIELDS) == {
        f.name for f in dataclasses.fields(solar.Machine)}


def test_grace_import_counts_only_grace_ticks():
    """Summing every tick would total ordinary night-time house import and the
    no-grid-electrons claim would mean nothing."""
    conn = db()
    solar.log_tick(conn, "V1", ts=100, state="idle", import_w=3000, period_s=120)
    solar.log_tick(conn, "V1", ts=220, state="grace", import_w=400, period_s=120)
    solar.log_tick(conn, "V1", ts=340, state="charging", import_w=0, period_s=120)
    wh = solar.grace_import_wh(conn, "V1", since_ts=0)
    assert wh == 400 * 120 / 3600
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_solar_store.py -v`
Expected: FAIL with `AttributeError: module 'solar' has no attribute 'SCHEMA'`

- [ ] **Step 3: Append persistence to `solar.py`**

Add `import sqlite3` and `import time` to the imports at the top of `solar.py`, then append:

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS solar_config (
  id                INTEGER PRIMARY KEY CHECK (id = 1),
  enabled           INTEGER NOT NULL DEFAULT 0,
  period_s          INTEGER NOT NULL DEFAULT 120,
  margin_w          INTEGER NOT NULL DEFAULT 100,
  deadband_w        INTEGER NOT NULL DEFAULT 250,
  ramp_a            INTEGER NOT NULL DEFAULT 8,
  min_a             INTEGER NOT NULL DEFAULT 5,
  grace_s           INTEGER NOT NULL DEFAULT 180,
  restart_hold_s    INTEGER NOT NULL DEFAULT 300,
  start_hold_s      INTEGER NOT NULL DEFAULT 60,
  raise_hold_s      INTEGER NOT NULL DEFAULT 600,
  soc_ceiling       INTEGER NOT NULL DEFAULT 90,
  raise_limit       INTEGER NOT NULL DEFAULT 1,
  -- 400/day is a RUNAWAY BACKSTOP, set above the ~250 expected on a charging
  -- day. It is not a budget enforcer. The earlier default of 1200 would have
  -- been $72/month against a $10 credit.
  daily_request_cap INTEGER NOT NULL DEFAULT 400,
  view_refresh_ticks INTEGER NOT NULL DEFAULT 5,
  deadline_soc      INTEGER,
  deadline_hour     INTEGER,
  updated_at        INTEGER NOT NULL DEFAULT 0
);

-- The machine's counters live here, not just its state name. Each tick is a
-- separate pass that reloads from the database, so a dwell counter held only
-- in memory would reset every time and the two-tick hysteresis would never
-- fire -- the exact flapping it exists to prevent.
CREATE TABLE IF NOT EXISTS solar_state (
  vin             TEXT PRIMARY KEY,
  state           TEXT    NOT NULL DEFAULT 'idle',
  breach_ticks    INTEGER NOT NULL DEFAULT 0,
  recover_ticks   INTEGER NOT NULL DEFAULT 0,
  grace_s_elapsed INTEGER NOT NULL DEFAULT 0,
  hold_s          INTEGER NOT NULL DEFAULT 0,
  dirty           INTEGER NOT NULL DEFAULT 0,
  original_amps   INTEGER,
  original_limit  INTEGER,
  raised_to       INTEGER,
  requests_today  INTEGER NOT NULL DEFAULT 0,
  requests_day    TEXT,
  capped          INTEGER NOT NULL DEFAULT 0,
  engaged_at      INTEGER,
  updated_at      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS solar_ticks (
  ts           INTEGER NOT NULL,
  vin          TEXT    NOT NULL,
  state        TEXT    NOT NULL,
  grid_w       REAL, solar_w REAL, car_w REAL, surplus_w REAL, error_w REAL,
  amps_before  INTEGER, amps_target INTEGER, amps_written INTEGER,
  soc          INTEGER,
  import_w     REAL,
  period_s     INTEGER,
  note         TEXT,
  PRIMARY KEY (vin, ts)
);
CREATE INDEX IF NOT EXISTS solar_ticks_state ON solar_ticks (vin, state, ts);
"""

CONFIG_DEFAULTS = {
    "enabled": 0, "period_s": 120, "margin_w": 100, "deadband_w": 250,
    "ramp_a": 8, "min_a": 5, "grace_s": 180, "restart_hold_s": 300,
    "start_hold_s": 60, "raise_hold_s": 600, "soc_ceiling": 90,
    "raise_limit": 1, "daily_request_cap": 400, "view_refresh_ticks": 5,
    "deadline_soc": None, "deadline_hour": None,
}

STATE_DEFAULTS = {
    "state": "idle", "breach_ticks": 0, "recover_ticks": 0,
    "grace_s_elapsed": 0, "hold_s": 0,
    "dirty": 0, "original_amps": None, "original_limit": None,
    "raised_to": None, "requests_today": 0, "requests_day": None,
    "capped": 0, "engaged_at": None,
}

# The Machine fields that must survive between ticks. Anything here that is
# not persisted silently disables the dwell and hysteresis logic.
MACHINE_FIELDS = ("state", "breach_ticks", "recover_ticks",
                  "grace_s_elapsed", "hold_s")


def machine_from(st: dict) -> Machine:
    return Machine(**{k: st[k] for k in MACHINE_FIELDS})


def machine_fields(m: Machine) -> dict:
    return {k: getattr(m, k) for k in MACHINE_FIELDS}


def load_config(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT * FROM solar_config WHERE id = 1").fetchone()
    if row is None:
        return dict(CONFIG_DEFAULTS)
    return {k: row[k] for k in CONFIG_DEFAULTS}


def save_config(db: sqlite3.Connection, **fields) -> None:
    unknown = set(fields) - set(CONFIG_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown solar_config fields: {sorted(unknown)}")
    current = load_config(db)
    current.update(fields)
    columns = list(CONFIG_DEFAULTS)
    db.execute(
        f"""INSERT INTO solar_config (id, {', '.join(columns)}, updated_at)
            VALUES (1, {', '.join('?' * len(columns))}, ?)
            ON CONFLICT(id) DO UPDATE SET
              {', '.join(f'{c} = excluded.{c}' for c in columns)},
              updated_at = excluded.updated_at""",
        [current[c] for c in columns] + [int(time.time())],
    )
    db.commit()


def tunables_from(cfg: dict, amps_max: int | None, volts: int | None) -> Tunables:
    """Config plus whatever the car actually reports.

    amps_max comes from charge_current_request_max (the session ceiling, which
    changes when the car is replugged elsewhere) and volts from charger_voltage,
    which is only meaningful mid-session. Both fall back to this site's measured
    nominal.
    """
    return Tunables(
        margin_w=cfg["margin_w"], deadband_w=cfg["deadband_w"],
        ramp_a=cfg["ramp_a"], min_a=cfg["min_a"],
        max_a=amps_max if amps_max else 48,
        volts=volts if volts else 240,
    )


def policy_from(cfg: dict) -> Policy:
    return Policy(grace_s=cfg["grace_s"], restart_hold_s=cfg["restart_hold_s"],
                  start_hold_s=cfg["start_hold_s"], enabled=bool(cfg["enabled"]))


def load_state(db: sqlite3.Connection, vin: str) -> dict:
    row = db.execute("SELECT * FROM solar_state WHERE vin = ?", (vin,)).fetchone()
    if row is None:
        return dict(STATE_DEFAULTS)
    return {k: row[k] for k in STATE_DEFAULTS}


def save_state(db: sqlite3.Connection, vin: str, **fields) -> None:
    unknown = set(fields) - set(STATE_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown solar_state fields: {sorted(unknown)}")
    current = load_state(db, vin)
    current.update(fields)
    columns = list(STATE_DEFAULTS)
    db.execute(
        f"""INSERT INTO solar_state (vin, {', '.join(columns)}, updated_at)
            VALUES (?, {', '.join('?' * len(columns))}, ?)
            ON CONFLICT(vin) DO UPDATE SET
              {', '.join(f'{c} = excluded.{c}' for c in columns)},
              updated_at = excluded.updated_at""",
        [vin] + [current[c] for c in columns] + [int(time.time())],
    )
    db.commit()


def log_tick(db: sqlite3.Connection, vin: str, **fields) -> None:
    """Every tick is logged, written or not, so a quiet loop and a broken loop
    look different in the record."""
    columns = ("ts", "state", "grid_w", "solar_w", "car_w", "surplus_w", "error_w",
               "amps_before", "amps_target", "amps_written", "soc", "import_w",
               "period_s", "note")
    db.execute(
        f"""INSERT OR REPLACE INTO solar_ticks (vin, {', '.join(columns)})
            VALUES (?, {', '.join('?' * len(columns))})""",
        [vin] + [fields.get(c) for c in columns],
    )
    db.commit()


def count_request(db: sqlite3.Connection, vin: str, today: str) -> tuple[int, bool]:
    """Increment the daily request counter. Returns (count, capped).

    Denominated in REQUESTS, not dollars: Tesla no longer publishes per-request
    data pricing, so a dollar cap would be a guess dressed as a limit.
    """
    st = load_state(db, vin)
    count = st["requests_today"] + 1 if st["requests_day"] == today else 1
    cap = load_config(db)["daily_request_cap"]
    capped = count >= cap
    save_state(db, vin, requests_today=count, requests_day=today,
               capped=1 if capped else 0)
    return count, capped


def grace_import_wh(db: sqlite3.Connection, vin: str, since_ts: int) -> float:
    """Watt-hours imported while riding out a cloud.

    ONLY grace ticks. Summing every tick would total ordinary house import and
    the no-grid-electrons promise would become unmeasurable.
    """
    row = db.execute(
        """SELECT COALESCE(SUM(import_w * period_s), 0) / 3600.0 AS wh
           FROM solar_ticks
           WHERE vin = ? AND state = 'grace' AND ts >= ? AND import_w > 0""",
        (vin, since_ts),
    ).fetchone()
    return float(row["wh"])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_solar_store.py -v`
Expected: 8 passed

- [ ] **Step 5: Register the schema with `Store`**

In `store.py`, add `import solar` at the top, and in `Store.__init__` after the `home.SCHEMA` line:

```python
        self._db.executescript(solar.SCHEMA)
```

- [ ] **Step 6: Run the full suite and commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

```bash
git add solar.py store.py tests/test_solar_store.py
git commit -m "feat: solar config, state, and tick persistence

The request cap is denominated in requests, not dollars: Tesla no longer
publishes per-request data pricing, so a dollar cap would be a guess.

Grace import sums only grace ticks -- totalling every tick would measure
ordinary house import and make the no-grid-electrons claim meaningless.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: `solar_routes.py` — the HTTP surface

**Files:**
- Create: `solar_routes.py`, `tests/test_solar_routes.py`
- Modify: `app.py` (register the router **before** the static mount at line 240)

**Interfaces:**
- Consumes: `solar`, `home`, `store.Store`.
- Produces:
  - `GET  /api/car/home` → `{"home": {lat, lon, radius_m} | null, "classification": str, "car": {lat, lon} | null}`
  - `PUT  /api/car/home` body `{latitude, longitude, radius_m}` → `{"ok": true}`
  - `GET  /api/car/solar/config` → the full config dict
  - `PUT  /api/car/solar/config` body = any subset of config fields → `{"ok": true}`
  - `GET  /api/car/solar/status` → `{state, surplus_w, amps, soc, limit, raised_to, original_limit, grace_import_wh_today, grace_import_wh_total, capped, dirty, engaged_at, last_tick_ts}`
  - `solar_routes.router: APIRouter`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_solar_routes.py`:

```python
from __future__ import annotations

import os

os.environ["DEMO"] = "1"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import solar_routes


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "db_file", tmp_path / "t.db")
    solar_routes._store = None
    app = FastAPI()
    app.include_router(solar_routes.router)
    return TestClient(app)


def test_home_is_null_until_set(client):
    body = client.get("/api/car/home").json()
    assert body["home"] is None
    assert body["classification"] == "unknown"


def test_home_round_trips(client):
    r = client.put("/api/car/home",
                   json={"latitude": 40.1672, "longitude": -105.1019, "radius_m": 120})
    assert r.status_code == 200
    body = client.get("/api/car/home").json()
    assert body["home"]["radius_m"] == 120
    assert body["home"]["latitude"] == pytest.approx(40.1672)


def test_home_rejects_out_of_range_coordinates(client):
    assert client.put("/api/car/home",
                      json={"latitude": 91, "longitude": 0, "radius_m": 100}).status_code == 400
    assert client.put("/api/car/home",
                      json={"latitude": 0, "longitude": 181, "radius_m": 100}).status_code == 400


def test_home_rejects_a_silly_radius(client):
    assert client.put("/api/car/home",
                      json={"latitude": 40, "longitude": -105, "radius_m": 5}).status_code == 400
    assert client.put("/api/car/home",
                      json={"latitude": 40, "longitude": -105, "radius_m": 99999}).status_code == 400


def test_config_defaults_then_partial_update(client):
    cfg = client.get("/api/car/solar/config").json()
    assert cfg["enabled"] == 0
    assert cfg["period_s"] == 120
    client.put("/api/car/solar/config", json={"enabled": 1, "soc_ceiling": 95})
    cfg = client.get("/api/car/solar/config").json()
    assert cfg["enabled"] == 1
    assert cfg["soc_ceiling"] == 95
    assert cfg["period_s"] == 120


def test_config_rejects_unknown_fields(client):
    r = client.put("/api/car/solar/config", json={"nonsense": 1})
    assert r.status_code == 400


def test_config_rejects_an_out_of_range_ceiling(client):
    assert client.put("/api/car/solar/config", json={"soc_ceiling": 49}).status_code == 400
    assert client.put("/api/car/solar/config", json={"soc_ceiling": 101}).status_code == 400


def test_config_rejects_a_period_below_the_meter_refresh(client):
    """grid_power refreshes at 60s. A faster loop reads the same number twice."""
    assert client.put("/api/car/solar/config", json={"period_s": 30}).status_code == 400


def test_status_reports_idle_before_anything_runs(client):
    body = client.get("/api/car/solar/status").json()
    assert body["state"] == "idle"
    assert body["grace_import_wh_total"] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_solar_routes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'solar_routes'`

- [ ] **Step 3: Write `solar_routes.py`**

```python
"""HTTP surface for home configuration and the solar controller.

This module never commands the car. The collector owns every write to the
vehicle; the web app only reads status and edits configuration, which the
collector picks up on its next tick. That keeps exactly one process issuing
commands and makes the signing proxy's per-VIN mutex a non-issue.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, HTTPException

import home
import solar
from config import settings
from store import Store

router = APIRouter(prefix="/api/car")

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(settings.db_file)
    return _store


def _vin() -> str:
    """The VIN the collector is recording under.

    Prefers the configured one; otherwise the most recent sample. Returns ""
    when nothing has ever been recorded, which every caller tolerates.
    """
    if settings.vin:
        return settings.vin
    row = store()._db.execute(
        "SELECT vin FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    return row["vin"] if row else ""


def _today() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")


def _midnight_ts() -> int:
    now = datetime.now(ZoneInfo(settings.timezone))
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


@router.get("/home")
async def get_home() -> dict[str, Any]:
    db = store()._db
    cfg = home.load(db)
    snap = store().snapshot(_vin()) if _vin() else None
    view = (snap or {}).get("view") or {}
    car = None
    if view.get("lat") is not None and view.get("lon") is not None:
        car = {"lat": view["lat"], "lon": view["lon"]}
    return {
        "home": None if cfg is None else {
            "latitude": cfg.latitude, "longitude": cfg.longitude,
            "radius_m": cfg.radius_m,
        },
        "classification": home.classify(view, cfg),
        "car": car,
    }


@router.put("/home")
async def put_home(body: dict[str, Any] = Body(...)) -> dict[str, bool]:
    try:
        lat = float(body["latitude"])
        lon = float(body["longitude"])
        radius = int(body["radius_m"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "latitude, longitude and radius_m are required")
    if not -90 <= lat <= 90:
        raise HTTPException(400, "latitude must be between -90 and 90")
    if not -180 <= lon <= 180:
        raise HTTPException(400, "longitude must be between -180 and 180")
    if not 10 <= radius <= 2000:
        raise HTTPException(400, "radius_m must be between 10 and 2000")
    home.save(store()._db, lat, lon, radius)
    return {"ok": True}


# Bounds are validation, not taste. period_s has a hard floor because
# grid_power only refreshes every 60 s; anything faster re-reads one value.
CONFIG_BOUNDS = {
    "enabled": (0, 1), "period_s": (60, 900), "margin_w": (0, 2000),
    "deadband_w": (50, 2000), "ramp_a": (1, 48), "min_a": (5, 32),
    "grace_s": (0, 3600), "restart_hold_s": (60, 3600),
    "start_hold_s": (0, 3600), "raise_hold_s": (0, 7200),
    "soc_ceiling": (50, 100), "raise_limit": (0, 1),
    "daily_request_cap": (0, 20000), "view_refresh_ticks": (1, 60),
    "deadline_soc": (0, 100),
    "deadline_hour": (0, 23),
}


@router.get("/solar/config")
async def get_solar_config() -> dict[str, Any]:
    return solar.load_config(store()._db)


@router.put("/solar/config")
async def put_solar_config(body: dict[str, Any] = Body(...)) -> dict[str, bool]:
    unknown = set(body) - set(CONFIG_BOUNDS)
    if unknown:
        raise HTTPException(400, f"unknown fields: {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for key, value in body.items():
        if value is None and key in ("deadline_soc", "deadline_hour"):
            clean[key] = None
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} must be an integer")
        low, high = CONFIG_BOUNDS[key]
        if not low <= number <= high:
            raise HTTPException(400, f"{key} must be between {low} and {high}")
        clean[key] = number
    solar.save_config(store()._db, **clean)
    return {"ok": True}


@router.get("/solar/status")
async def get_solar_status() -> dict[str, Any]:
    db = store()._db
    vin = _vin()
    state = solar.load_state(db, vin) if vin else dict(solar.STATE_DEFAULTS)
    snap = store().snapshot(vin) if vin else None
    view = (snap or {}).get("view") or {}
    last = db.execute(
        "SELECT ts, surplus_w, amps_written, amps_before FROM solar_ticks"
        " WHERE vin = ? ORDER BY ts DESC LIMIT 1", (vin,)).fetchone() if vin else None
    return {
        "state": state["state"],
        "surplus_w": last["surplus_w"] if last else None,
        "amps": (last["amps_written"] or last["amps_before"]) if last else None,
        "soc": view.get("soc"),
        "limit": view.get("limit"),
        "raised_to": state["raised_to"],
        "original_limit": state["original_limit"],
        "grace_import_wh_today": round(
            solar.grace_import_wh(db, vin, _midnight_ts()), 1) if vin else 0,
        "grace_import_wh_total": round(
            solar.grace_import_wh(db, vin, 0), 1) if vin else 0,
        "capped": bool(state["capped"]),
        "dirty": bool(state["dirty"]),
        "engaged_at": state["engaged_at"],
        "last_tick_ts": last["ts"] if last else None,
        "requests_today": state["requests_today"] if state["requests_day"] == _today() else 0,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_solar_routes.py -v`
Expected: 9 passed

- [ ] **Step 5: Register the router in `app.py`**

Add the import beside the existing `import car_routes` (line 21):

```python
import solar_routes
```

And immediately after `app.include_router(car_routes.router)` (line 236), before the comment about the static mount:

```python
app.include_router(solar_routes.router)
```

- [ ] **Step 6: Verify the routes are reachable on the running app**

```bash
lsof -ti:8000 | xargs kill -9 2>/dev/null
cd /Users/d/Code/tesla_automation && nohup .venv/bin/python app.py > /tmp/app.log 2>&1 &
sleep 4
curl -s localhost:8000/api/car/solar/config | head -c 300; echo
curl -s localhost:8000/api/car/home | head -c 300; echo
```

Expected: both return JSON, not the static handler's 404. If either returns HTML, the router was registered after the mount.

- [ ] **Step 7: Run the full suite and commit**

```bash
git add solar_routes.py app.py tests/test_solar_routes.py
git commit -m "feat: HTTP surface for home config and solar status

The web app never commands the car; the collector owns every vehicle write,
so exactly one process contends for the signing proxy's per-VIN mutex.

Router is registered before the static mount, which is a catch-all.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Wire the loop into the collector

This is where pure logic meets the car. Read spec §3.3, §3.5 and §3.6 before starting.

**Files:**
- Modify: `collector.py`, `solar.py` (add `may_restore`), `tesla.py` (add `proxy_up`), `car_routes.py:229-239` (delegate to it)
- Test: `tests/test_collector_solar.py` (create), `tests/test_solar_state.py` (extend)

**Interfaces:**
- Consumes: everything from Tasks 3-7.
- Produces: `tesla.proxy_up(proxy_url: str) -> bool` — blocking; call via `asyncio.to_thread`.
- Produces:
  - `collector.next_interval(car_state, view, cfg, solar_engaged: bool = False) -> int`
  - `collector.recover(db, client, vin, view, location) -> bool` — the §3.6 crash-recovery gate; returns True when the state is clean
  - `collector.solar_tick(client, store_, vin, view, cfg) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_collector_solar.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

import collector

S = SimpleNamespace(poll_driving=120, poll_charging=300, poll_idle=900,
                    poll_asleep=300)


def test_engaged_uses_the_solar_period_over_every_other_rule():
    assert collector.next_interval(
        "online", {"shift": "P", "charging": True}, S, solar_engaged=120) == 120


def test_not_engaged_keeps_the_existing_behaviour():
    assert collector.next_interval(
        "online", {"shift": "P", "charging": True}, S, solar_engaged=0) == 300
    assert collector.next_interval("offline", None, S, solar_engaged=0) == 300


def test_engagement_never_overrides_a_sleeping_car():
    """A sleeping car must not be polled fast; the loop idles instead."""
    assert collector.next_interval("asleep", None, S, solar_engaged=120) == 300
```

Add to `tests/test_solar_state.py`:

```python
def test_recovery_gate_refuses_to_restore_away_from_home():
    """An unconditional restore would write home amps into a Supercharger
    session -- the exact hazard the home gate exists to prevent."""
    assert solar.may_restore(dirty=1, location="away", online=True, proxy_up=True) is False
    assert solar.may_restore(dirty=1, location="unknown", online=True, proxy_up=True) is False
    assert solar.may_restore(dirty=1, location="home", online=False, proxy_up=True) is False
    assert solar.may_restore(dirty=1, location="home", online=True, proxy_up=False) is False
    assert solar.may_restore(dirty=1, location="home", online=True, proxy_up=True) is True
    assert solar.may_restore(dirty=0, location="home", online=True, proxy_up=True) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_collector_solar.py tests/test_solar_state.py -v`
Expected: FAIL — `next_interval() got an unexpected keyword argument 'solar_engaged'` and `module 'solar' has no attribute 'may_restore'`.

- [ ] **Step 3: Add the recovery gate to `solar.py`**

```python
def may_restore(dirty: int, location: str, online: bool, proxy_up: bool) -> bool:
    """Whether a crash-recovery restore may be attempted right now.

    Restoring writes amps and a charge limit to the car. Doing that
    unconditionally at startup would push home settings into whatever session
    the car is actually in -- including a Supercharger. If any gate fails the
    dirty flag STAYS SET and we retry next tick; it is never cleared by
    giving up.
    """
    return bool(dirty) and location == "home" and online and proxy_up
```

- [ ] **Step 4: Change `next_interval` in `collector.py`**

Replace the function with:

```python
def next_interval(car_state: str, view: dict | None, cfg,
                  solar_engaged: int = 0) -> int:
    """Seconds until the next poll. Pure, so it is testable without a car.

    `solar_engaged` is the solar period in seconds when the controller holds
    the car, else 0. It wins over every other cadence EXCEPT sleep: a sleeping
    car is never polled fast, because the loop cannot act on it anyway.
    """
    if car_state != "online":
        return cfg.poll_asleep
    if solar_engaged:
        return solar_engaged
    if view is None:
        return cfg.poll_idle
    if view.get("shift") in DRIVING:
        return cfg.poll_driving
    if view.get("charging"):
        return cfg.poll_charging
    return cfg.poll_idle
```

- [ ] **Step 5: Lift `proxy_up` into `tesla.py` so both callers share it**

`car_routes.py:229` already has this helper, and `car_routes.py:220` calls it
through `asyncio.to_thread` because a prior review caught the blocking socket
sitting in an async handler. The collector needs the same check, so move it
rather than copy it.

Add to `tesla.py` at module level (it already imports `socket` and `urlparse`;
add them if not):

```python
def proxy_up(proxy_url: str) -> bool:
    """TCP reachability of the signing proxy. BLOCKING -- callers on an event
    loop must use asyncio.to_thread. Commands are impossible without the proxy,
    so this gates anything that would write to the car."""
    parsed = urlparse(proxy_url)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 443), timeout=0.5
        ):
            return True
    except OSError:
        return False
```

Then replace the body of `car_routes._proxy_up` (lines 229-239) with a
delegation, keeping its existing signature so line 220 is untouched:

```python
def _proxy_up() -> bool:
    return tesla.proxy_up(settings.proxy_url)
```

and add `import tesla` to `car_routes.py`'s imports. Run
`.venv/bin/python -m pytest tests/test_car_routes.py -v` — it must stay green.

- [ ] **Step 6: Add the solar tick to `collector.py`**

Add these imports at the top of `collector.py`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

import home
import solar
import tesla
```

Then append these functions above `run()`:

```python
ENGAGED_STATES = {"charging", "grace", "stopped"}


async def _command(client: TeslaClient, vin: str, name: str, **params) -> bool:
    """Issue one signed command. Returns True only when the car accepted it.

    client.command() returns a (status, body) TUPLE, and the proxy answers 200
    with result:false for a refusal. Treating any non-exception as success
    would let the controller integrate against an amps value the car never
    adopted -- the exact failure invariant 2 of the spec exists to prevent.
    """
    try:
        status, body = await client.command(vin, name, params)
    except (TeslaAPIError, TeslaAuthError) as exc:
        _log(f"command {name} failed: {exc}")
        return False
    if status != 200:
        _log(f"command {name} rejected: HTTP {status}")
        return False
    result = (body or {}).get("response") or {}
    if result.get("result") is False:
        _log(f"command {name} refused: {result.get('reason')}")
        return False
    return True


async def solar_tick(client: TeslaClient, store_: Store, vin: str,
                     view: dict, cfg) -> str:
    """One control iteration. Returns the resulting state name.

    Ordering matters: read the meter, decide, act, log. The tick is logged
    whether or not anything was written, so a quiet loop and a broken loop look
    different in solar_ticks.
    """
    db = store_._db
    conf = solar.load_config(db)
    st = solar.load_state(db, vin)
    home_cfg = home.load(db)
    location = home.classify(view, home_cfg)

    today = datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d")

    # --- crash recovery, before anything else can engage -------------------
    if st["dirty"]:
        # to_thread because proxy_up opens a blocking socket. car_routes made
        # exactly this mistake and its review caught it; do not reintroduce it.
        proxy_ok = await asyncio.to_thread(tesla.proxy_up, cfg.proxy_url)
        if solar.may_restore(st["dirty"], location, True, proxy_ok):
            if st["original_amps"] is not None:
                await _command(client, vin, "set_charging_amps",
                               charging_amps=st["original_amps"])
            if st["original_limit"] is not None:
                await _command(client, vin, "set_charge_limit",
                               percent=st["original_limit"])
            solar.save_state(db, vin, dirty=0, raised_to=None)
            _log(f"recovered: restored amps={st['original_amps']} "
                 f"limit={st['original_limit']}")
        else:
            _log(f"dirty, cannot restore yet (location={location})")
            return st["state"]
        st = solar.load_state(db, vin)

    if not conf["enabled"] and st["state"] == "idle":
        return "idle"

    # --- read the meter ----------------------------------------------------
    count, capped = solar.count_request(db, vin, today)
    if capped:
        _log(f"daily request cap reached ({count}); pausing")
        if st["state"] != "idle":
            await _restore(client, db, vin, st)
        return "idle"

    try:
        sites = await client.energy_sites()
        live = await client._get(
            f"/api/1/energy_sites/{sites[0]['energy_site_id']}/live_status", ttl=0)
    except (TeslaAPIError, TeslaAuthError, IndexError, KeyError) as exc:
        _log(f"live_status failed: {exc}; holding")
        return st["state"]

    grid_w = live.get("grid_power")
    if grid_w is None:
        _log("grid_power absent; skipping tick")
        return st["state"]
    grid_w = float(grid_w)

    # --- decide ------------------------------------------------------------
    tun = solar.tunables_from(conf, view.get("amps_max"), view.get("volts"))
    current_a = view.get("amps_actual") or view.get("charge_amps") or tun.min_a
    car_w = solar.car_watts(view, tun.volts)
    surplus_w = solar.surplus_watts(car_w, grid_w)
    decision = solar.control(grid_w, int(current_a), tun)

    # Rebuild the WHOLE machine from the database, not just its state name --
    # the dwell and hysteresis counters are what make it stable across ticks.
    machine = solar.machine_from(st)
    tick = solar.Tick(
        surplus_w=surplus_w, decision=decision, location=location,
        plugged=view.get("charging_state") not in (None, "Disconnected"),
        period_s=conf["period_s"])
    machine, actions = solar.advance(machine, tick, solar.policy_from(conf), tun)

    # --- act ---------------------------------------------------------------
    written = None
    for action in actions:
        if action == "restore":
            await _restore(client, db, vin, st)
        elif action == "wake":
            # wake_up is a dedicated REST endpoint, NOT a signed command --
            # routing it through the proxy would 400. tesla.py:386.
            try:
                await client.wake_up(vin)
                await asyncio.sleep(5)
            except (TeslaAPIError, TeslaAuthError) as exc:
                _log(f"wake failed: {exc}")
                return st["state"]
        elif action == "charge_start":
            if st["original_amps"] is None:      # remember BEFORE we change it
                solar.save_state(db, vin, dirty=1,
                                 original_amps=view.get("charge_amps"),
                                 original_limit=view.get("limit"),
                                 engaged_at=int(time.time()))
                st = solar.load_state(db, vin)
            await _command(client, vin, "charge_start")
        elif action == "charge_stop":
            await _command(client, vin, "charge_stop")
        elif action == "set_amps":
            target = tun.min_a if machine.state == "grace" else decision.target_a
            if await _command(client, vin, "set_charging_amps",
                              charging_amps=target):
                written = target

    solar.save_state(db, vin, **solar.machine_fields(machine))
    solar.log_tick(db, vin, ts=int(time.time()), state=machine.state,
                   grid_w=grid_w, solar_w=live.get("solar_power"), car_w=car_w,
                   surplus_w=surplus_w, error_w=decision.error_w,
                   amps_before=int(current_a), amps_target=decision.target_a,
                   amps_written=written, soc=view.get("soc"),
                   import_w=max(grid_w, 0.0), period_s=conf["period_s"])
    _log(f"solar {machine.state} surplus={surplus_w:.0f}W "
         f"amps={current_a}->{written if written is not None else '-'}")
    return machine.state


async def _restore(client: TeslaClient, db, vin: str, st: dict) -> None:
    """Put back whatever we changed, then clear dirty."""
    if st["original_amps"] is not None:
        await _command(client, vin, "set_charging_amps",
                       charging_amps=st["original_amps"])
    if st["original_limit"] is not None:
        await _command(client, vin, "set_charge_limit", percent=st["original_limit"])
    solar.save_state(db, vin, dirty=0, original_amps=None, original_limit=None,
                     raised_to=None, engaged_at=None)
```

- [ ] **Step 7: Call it from the run loop, with the budget call pattern**

Read spec §1.7.1 before writing this. The naive version — `poll_once` every
tick, then `live_status` — makes **three** billable calls per tick and costs
**$21.78/month** at a 120 s period, against a $10 credit. The pattern below
brings it to ~1.4 calls/tick and ~$13. This is a requirement, not a tuning
opportunity.

Two savings, both safe:

- **No state check while engaged.** `poll_once`'s cheap `/vehicles/{vin}` call
  exists so we don't pay for a `vehicle_data` that is going to 408. While
  engaged the car is demonstrably awake — it is charging. If `vehicle_data`
  later 408s, that *is* the signal it slept.
- **`vehicle_data` only when it is needed:** after an amps write (the §3.7.2
  readback invariant), every `view_refresh_ticks` (default 5) to catch unplug
  and drive-away, and on any state transition. Between those, reuse the last
  view and the last acknowledged amps.

First change `poll_once` to stamp the location classification:

```python
    store.record(view, at_home=home.classify(view, home.load(store._db)))
    return car_state, view
```

Then add a helper beside it:

```python
async def refresh_view(client: TeslaClient, store_: Store, vin: str):
    """A paid vehicle_data read with no preceding state check.

    Only called when the car is already known awake. Returns None if it turns
    out to be asleep after all -- which is the 408 doing the state check's job
    for free.
    """
    try:
        view = vehicle.derive(await client.vehicle_data(vin))
    except VehicleAsleep:
        return None
    except TeslaAPIError as exc:
        _log(f"vehicle_data failed: {exc}")
        return None
    store_.record(view, at_home=home.classify(view, home.load(store_._db)))
    return view
```

Now replace the body of `run()`'s `while True:` block with:

```python
            conf = solar.load_config(store._db)
            st = solar.load_state(store._db, vin)
            solar_wanted = bool(conf["enabled"]) or bool(st["dirty"])

            try:
                if engaged and view is not None:
                    # Awake by definition. Skip the state check, and only pay
                    # for vehicle_data when this tick actually needs it.
                    stale = ticks_since_view >= conf["view_refresh_ticks"]
                    if stale or wrote_last_tick:
                        fresh = await refresh_view(client, store, vin)
                        if fresh is None:
                            car_state, view, engaged = "asleep", None, 0
                        else:
                            view, ticks_since_view = fresh, 0
                    else:
                        ticks_since_view += 1
                else:
                    car_state, view = await poll_once(client, store, vin, settings)
                    ticks_since_view = 0
            except TeslaAuthError as exc:
                _log(f"auth lost: {exc}")
                return 1

            wrote_last_tick = False
            if view is not None and solar_wanted:
                state, wrote_last_tick = await solar_tick(
                    client, store, vin, view, settings)
                engaged = conf["period_s"] if state in ENGAGED_STATES else 0
            else:
                engaged = 0

            soc = (view or {}).get("soc")
            _log(f"{car_state}" + (f" soc={soc}%" if soc is not None else ""))
            if once:
                return 0
            await asyncio.sleep(next_interval(car_state, view, settings, engaged))
```

Initialise the three carried variables immediately above `while True:`:

```python
    engaged, ticks_since_view, wrote_last_tick = 0, 0, False
    car_state, view = "offline", None
```

Finally, change `solar_tick` to return `(state, wrote)` rather than just the
state — the loop needs to know whether an amps write happened so it can force
the readback next tick. Its `return` statements become:

```python
    return machine.state, written is not None
```

and the early returns become `return st["state"], False`.

**Add a test** to `tests/test_collector_solar.py` proving the saving is real:

```python
def test_engaged_ticks_skip_the_state_check_and_most_vehicle_data(monkeypatch):
    """Three billable calls per tick is $22/month; this pattern is ~1.4."""
    calls = []

    class FakeClient:
        async def vehicle(self, vin):
            calls.append("state"); return {"state": "online"}
        async def vehicle_data(self, vin, *a, **k):
            calls.append("data"); return {"response": {}}

    # With view_refresh_ticks=5 and no writes, 5 engaged ticks must issue
    # exactly ONE vehicle_data and ZERO state checks.
    assert collector.should_refresh_view(ticks_since_view=0, refresh_every=5,
                                         wrote_last_tick=False) is False
    assert collector.should_refresh_view(ticks_since_view=5, refresh_every=5,
                                         wrote_last_tick=False) is True
    assert collector.should_refresh_view(ticks_since_view=0, refresh_every=5,
                                         wrote_last_tick=True) is True
```

which requires extracting the predicate as a pure function in `collector.py`:

```python
def should_refresh_view(ticks_since_view: int, refresh_every: int,
                        wrote_last_tick: bool) -> bool:
    """Whether this engaged tick must pay for a vehicle_data read.

    Every avoidable call is $0.002 against a $10/month credit. See spec
    section 1.7.1.
    """
    return wrote_last_tick or ticks_since_view >= refresh_every
```

and using it in the loop in place of the inline `stale or wrote_last_tick`.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_collector_solar.py tests/test_solar_state.py -v`
Expected: all pass.

- [ ] **Step 9: Run one real tick with the controller disabled**

```bash
.venv/bin/python collector.py --once
```

Expected: normal collector output, **no** `solar` line — `enabled` defaults to 0, so the loop must not engage or spend a single command.

- [ ] **Step 10: Run the full suite and commit**

```bash
git add collector.py solar.py tests/test_collector_solar.py tests/test_solar_state.py
git commit -m "feat: run the solar control loop inside the collector

One background process rather than two: a single vehicle_data poll serves both
history and control, the signing proxy's per-VIN mutex cannot be contended,
and only one process besides the web app touches tokens.

Crash recovery is gated on being home, online, and proxy-reachable; failing a
gate leaves dirty set to retry rather than clearing it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: The setup page

**Files:**
- Create: `static/setup.html`, `static/setup.js`
- Modify: `static/styles.css` (append), `static/car.html` (add a nav link)

**Interfaces:**
- Consumes: `/api/car/home`, `/api/car/solar/config` from Task 8.
- Produces: a page at `/setup.html`.

- [ ] **Step 1: Write `static/setup.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Setup — Tesla</title>
  <link rel="stylesheet" href="/vendor/leaflet.css">
  <link rel="stylesheet" href="/styles.css">
</head>
<body>
  <div id="setup-app">
    <header class="topbar">
      <h1>Setup</h1>
      <nav><a href="/car.html">Car</a> <a href="/index.html">Solar</a></nav>
    </header>

    <section class="card">
      <h2>Home</h2>
      <p class="hint">Drag the pin to where the car actually parks — the driveway,
        not the middle of the house. The circle is the geofence.</p>
      <div id="map" class="map"></div>
      <div class="row">
        <label for="radius">Radius</label>
        <input type="range" id="radius" min="25" max="500" step="5" value="100">
        <output id="radius-out">100 m</output>
      </div>
      <p>Car is currently: <strong id="classification">unknown</strong></p>
      <button id="save-home">Save home</button>
      <span id="home-msg" class="msg"></span>
    </section>

    <section class="card">
      <h2>Solar charging</h2>
      <label class="row"><input type="checkbox" id="enabled"> Match charge rate to solar export</label>

      <div class="row">
        <label for="period">Loop period</label>
        <select id="period">
          <option value="120">120 s (recommended)</option>
          <option value="60">60 s (tracks clouds harder, costs more)</option>
        </select>
      </div>

      <div class="row">
        <label for="ceiling">Charge limit ceiling</label>
        <input type="range" id="ceiling" min="50" max="100" step="5" value="90">
        <output id="ceiling-out">90%</output>
      </div>
      <p class="hint">Raised only while surplus is actually being exported, and put
        back when the sun goes. Sitting near 100% is what ages the pack, not
        reaching it.</p>
      <label class="row"><input type="checkbox" id="raise-limit" checked>
        Allow raising the limit to capture more sun</label>

      <div class="row">
        <label for="grace">Cloud grace</label>
        <input type="number" id="grace" min="0" max="20" step="1" value="3">
        <span>minutes</span>
      </div>
      <p class="hint">Below about 1.3 kW the car cannot charge at all. Grace holds at
        the minimum through short clouds instead of stopping — which imports a little.
        Set to 0 to never import.</p>

      <details>
        <summary>Tuning</summary>
        <div class="row"><label for="margin">Export margin (W)</label>
          <input type="number" id="margin" min="0" max="2000" step="10" value="100"></div>
        <div class="row"><label for="deadband">Deadband (W)</label>
          <input type="number" id="deadband" min="50" max="2000" step="10" value="250"></div>
        <div class="row"><label for="ramp">Ramp limit (A/tick)</label>
          <input type="number" id="ramp" min="1" max="48" step="1" value="8"></div>
        <div class="row"><label for="mina">Minimum amps</label>
          <input type="number" id="mina" min="5" max="32" step="1" value="5"></div>
        <div class="row"><label for="cap">Daily request cap</label>
          <input type="number" id="cap" min="0" max="20000" step="50" value="400"></div>
      </details>

      <button id="save-solar">Save</button>
      <span id="solar-msg" class="msg"></span>
    </section>

    <section class="card">
      <h2>Deadline warning</h2>
      <p class="hint">Never acts — it only tells you, once the sun is done for the day,
        that solar alone did not reach your target.</p>
      <div class="row">
        <label for="dsoc">Want at least</label>
        <input type="number" id="dsoc" min="0" max="100" step="5" placeholder="off">
        <span>% by</span>
        <input type="number" id="dhour" min="0" max="23" step="1" placeholder="7">
        <span>:00</span>
      </div>
      <button id="save-deadline">Save</button>
      <span id="deadline-msg" class="msg"></span>
    </section>
  </div>

  <script src="/vendor/leaflet.js"></script>
  <script src="/shared.js"></script>
  <script src="/setup.js"></script>
</body>
</html>
```

- [ ] **Step 2: Write `static/setup.js`**

```js
/* Setup page: home pin + geofence, and the solar controller's configuration.
   No build step — this file is served as-is. */
"use strict";

const $ = (id) => document.getElementById(id);
const DEFAULT_CENTER = [39.8283, -98.5795]; // geographic centre of the US
let map, marker, circle;

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function flash(el, message, ok) {
  el.textContent = message;
  el.className = "msg " + (ok ? "ok" : "err");
  setTimeout(() => { el.textContent = ""; }, 4000);
}

function drawGeofence(latlng, radius) {
  if (!marker) {
    marker = L.marker(latlng, { draggable: true }).addTo(map);
    marker.on("drag", () => circle.setLatLng(marker.getLatLng()));
  } else {
    marker.setLatLng(latlng);
  }
  if (!circle) {
    circle = L.circle(latlng, { radius, color: "#3b82f6", weight: 1 }).addTo(map);
  } else {
    circle.setLatLng(latlng).setRadius(radius);
  }
}

async function loadHome() {
  const body = await api("/api/car/home");
  const radius = body.home ? body.home.radius_m : 100;
  // Seed from the saved pin, else the car's current position, else nowhere
  // useful — the user drags it either way.
  const centre = body.home
    ? [body.home.latitude, body.home.longitude]
    : body.car ? [body.car.lat, body.car.lon] : DEFAULT_CENTER;

  map = L.map("map").setView(centre, body.home || body.car ? 16 : 4);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors", maxZoom: 19,
  }).addTo(map);
  drawGeofence(centre, radius);

  $("radius").value = radius;
  $("radius-out").textContent = radius + " m";
  $("classification").textContent = body.classification;
}

$("radius").addEventListener("input", (e) => {
  const r = Number(e.target.value);
  $("radius-out").textContent = r + " m";
  if (circle) circle.setRadius(r);
});

$("save-home").addEventListener("click", async () => {
  try {
    const p = marker.getLatLng();
    await api("/api/car/home", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        latitude: p.lat, longitude: p.lng, radius_m: Number($("radius").value),
      }),
    });
    const body = await api("/api/car/home");
    $("classification").textContent = body.classification;
    flash($("home-msg"), "Saved", true);
  } catch (err) {
    flash($("home-msg"), err.message, false);
  }
});

async function loadSolar() {
  const c = await api("/api/car/solar/config");
  $("enabled").checked = !!c.enabled;
  $("period").value = String(c.period_s);
  $("ceiling").value = c.soc_ceiling;
  $("ceiling-out").textContent = c.soc_ceiling + "%";
  $("raise-limit").checked = !!c.raise_limit;
  $("grace").value = Math.round(c.grace_s / 60);
  $("margin").value = c.margin_w;
  $("deadband").value = c.deadband_w;
  $("ramp").value = c.ramp_a;
  $("mina").value = c.min_a;
  $("cap").value = c.daily_request_cap;
  $("dsoc").value = c.deadline_soc === null ? "" : c.deadline_soc;
  $("dhour").value = c.deadline_hour === null ? "" : c.deadline_hour;
}

$("ceiling").addEventListener("input", (e) => {
  $("ceiling-out").textContent = e.target.value + "%";
});

$("save-solar").addEventListener("click", async () => {
  try {
    await api("/api/car/solar/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: $("enabled").checked ? 1 : 0,
        period_s: Number($("period").value),
        soc_ceiling: Number($("ceiling").value),
        raise_limit: $("raise-limit").checked ? 1 : 0,
        grace_s: Number($("grace").value) * 60,
        margin_w: Number($("margin").value),
        deadband_w: Number($("deadband").value),
        ramp_a: Number($("ramp").value),
        min_a: Number($("mina").value),
        daily_request_cap: Number($("cap").value),
      }),
    });
    flash($("solar-msg"), "Saved", true);
  } catch (err) {
    flash($("solar-msg"), err.message, false);
  }
});

$("save-deadline").addEventListener("click", async () => {
  try {
    const soc = $("dsoc").value === "" ? null : Number($("dsoc").value);
    const hour = $("dhour").value === "" ? null : Number($("dhour").value);
    await api("/api/car/solar/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deadline_soc: soc, deadline_hour: hour }),
    });
    flash($("deadline-msg"), "Saved", true);
  } catch (err) {
    flash($("deadline-msg"), err.message, false);
  }
});

(async function init() {
  try {
    await loadHome();
    await loadSolar();
  } catch (err) {
    console.error(err);
  }
})();
```

- [ ] **Step 3: Add the styles**

Append to `static/styles.css`:

```css
/* ---- setup page ---- */
#setup-app { max-width: 46rem; margin: 0 auto; padding: 1rem; }
#setup-app .map { height: 22rem; border-radius: var(--radius, 8px); margin: .75rem 0; }
#setup-app .row { display: flex; align-items: center; gap: .6rem; margin: .5rem 0; }
#setup-app .row label { min-width: 11rem; }
#setup-app .hint { color: var(--muted, #888); font-size: .85rem; margin: .35rem 0 .6rem; }
#setup-app details { margin: .75rem 0; }
#setup-app details summary { cursor: pointer; color: var(--muted, #888); }
#setup-app .msg { margin-left: .6rem; font-size: .9rem; }
#setup-app .msg.ok { color: var(--good, #22c55e); }
#setup-app .msg.err { color: var(--bad, #ef4444); }
```

- [ ] **Step 4: Link it from the car page**

In `static/car.html`, add `<a href="/setup.html">Setup</a>` to the existing nav element.

- [ ] **Step 5: Verify in a browser**

```bash
lsof -ti:8000 | xargs kill -9 2>/dev/null
cd /Users/d/Code/tesla_automation && nohup .venv/bin/python app.py > /tmp/app.log 2>&1 &
sleep 4
open http://localhost:8000/setup.html
```

Check, and fix anything that fails:
1. The map renders with OSM tiles and an attribution control.
2. Dragging the pin moves the circle with it.
3. The radius slider resizes the circle live and the readout updates.
4. **Save home** shows "Saved", and reloading the page restores the pin and radius.
5. The classification line reads `home`, `away` or `unknown` — never blank.
6. Saving an invalid value (set radius via devtools to 5) surfaces the API's error text, not a silent failure.
7. Dark mode: the page follows the existing theme.

- [ ] **Step 6: Commit**

```bash
git add static/setup.html static/setup.js static/styles.css static/car.html
git commit -m "feat: setup page for home geofence and solar configuration

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Solar status card and demo fixtures

**Files:**
- Modify: `static/car.js`, `static/car.html`, `static/styles.css`, `demo.py`, `solar_routes.py`
- Test: `tests/test_solar_routes.py` (extend)

**Interfaces:**
- Consumes: `/api/car/solar/status`.
- Produces: `demo.solar_status()` and `demo.home()` returning fixture payloads.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_solar_routes.py`:

```python
def test_demo_status_reports_a_live_session(client):
    """DEMO=1 must render every field the card shows, without a car."""
    body = client.get("/api/car/solar/status").json()
    assert body["state"] in {"idle", "charging", "grace", "stopped"}
    for key in ("surplus_w", "amps", "soc", "grace_import_wh_today",
                "capped", "dirty", "raised_to", "original_limit"):
        assert key in body, key
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_solar_routes.py::test_demo_status_reports_a_live_session -v`
Expected: FAIL — keys missing because the demo branch does not exist yet.

- [ ] **Step 3: Add the demo fixtures**

Append to `demo.py`:

```python
def solar_status() -> dict:
    """A mid-session solar charge, so the card can be built without a car."""
    return {
        "state": "charging",
        "surplus_w": 6240.0,
        "amps": 26,
        "soc": 72,
        "limit": 90,
        "raised_to": 90,
        "original_limit": 80,
        "grace_import_wh_today": 41.3,
        "grace_import_wh_total": 512.8,
        "capped": False,
        "dirty": False,
        "engaged_at": int(time.time()) - 4200,
        "last_tick_ts": int(time.time()) - 40,
        "requests_today": 173,
    }


def home_config() -> dict:
    return {
        "home": {"latitude": 40.1672, "longitude": -105.1019, "radius_m": 100},
        "classification": "home",
        "car": {"lat": 40.1673, "lon": -105.1018},
    }
```

Confirm `import time` is present at the top of `demo.py`; add it if not.

- [ ] **Step 4: Branch the routes on DEMO**

In `solar_routes.py`, add near the imports:

```python
import os

import demo

DEMO = os.getenv("DEMO", "").strip() in {"1", "true", "yes"}
```

Add as the first line of `get_solar_status`:

```python
    if DEMO:
        return demo.solar_status()
```

and as the first line of `get_home`:

```python
    if DEMO:
        return demo.home_config()
```

- [ ] **Step 5: Add the card markup**

In `static/car.html`, insert before the controls section:

```html
    <section class="card" id="solar-card" hidden>
      <h2>Solar charging</h2>
      <div class="solar-head">
        <span id="solar-state" class="pill">idle</span>
        <span id="solar-surplus" class="big">—</span>
        <span id="solar-amps" class="muted">—</span>
      </div>
      <p id="solar-limit-note" class="hint" hidden></p>
      <p id="solar-grace" class="hint"></p>
      <p id="solar-warn" class="warn" hidden></p>
    </section>
```

- [ ] **Step 6: Render it in `static/car.js`**

Add this function and call it from the existing refresh routine:

```js
async function loadSolar() {
  let s;
  try {
    s = await fetchJSON("/api/car/solar/status");
  } catch (_) {
    return;                       // the card simply stays hidden
  }
  const card = $("solar-card");
  card.hidden = false;

  $("solar-state").textContent = s.state;
  $("solar-state").className = "pill state-" + s.state;
  $("solar-surplus").textContent =
    s.surplus_w === null ? "—" : (s.surplus_w / 1000).toFixed(2) + " kW surplus";
  $("solar-amps").textContent = s.amps === null ? "" : s.amps + " A";

  // Show the raised limit NEXT TO the original, so a stuck raise is visible
  // rather than something you discover next month.
  const note = $("solar-limit-note");
  if (s.raised_to && s.original_limit && s.raised_to !== s.original_limit) {
    note.textContent = `Charge limit raised to ${s.raised_to}% for solar `
                     + `(your setting: ${s.original_limit}%)`;
    note.hidden = false;
  } else {
    note.hidden = true;
  }

  $("solar-grace").textContent =
    `Imported while riding out clouds: ${s.grace_import_wh_today} Wh today, `
    + `${s.grace_import_wh_total} Wh total`;

  const warn = $("solar-warn");
  if (s.capped) {
    warn.textContent = "Paused: daily API request cap reached. Resumes tomorrow.";
    warn.hidden = false;
  } else if (s.dirty) {
    warn.textContent = "Your original charge settings have not been restored yet — "
                     + "waiting for the car to be home and reachable.";
    warn.hidden = false;
  } else {
    warn.hidden = true;
  }
}
```

- [ ] **Step 7: Style the card**

Append to `static/styles.css`:

```css
#solar-card .solar-head { display: flex; align-items: baseline; gap: .75rem; }
#solar-card .big { font-size: 1.6rem; font-weight: 600; }
#solar-card .pill {
  padding: .15rem .55rem; border-radius: 999px; font-size: .8rem;
  background: var(--chip, #33333322);
}
#solar-card .pill.state-charging { background: var(--good, #22c55e); color: #fff; }
#solar-card .pill.state-grace    { background: var(--warn, #f59e0b); color: #fff; }
#solar-card .warn { color: var(--bad, #ef4444); }
```

- [ ] **Step 8: Verify in a browser under DEMO**

```bash
lsof -ti:8123 | xargs kill -9 2>/dev/null
cd /Users/d/Code/tesla_automation && DEMO=1 PORT=8123 nohup .venv/bin/python app.py > /tmp/demo.log 2>&1 &
sleep 4
open http://localhost:8123/car.html
```

Check: the card shows `charging`, `6.24 kW surplus`, `26 A`; the limit note reads "raised to 90% for solar (your setting: 80%)"; the grace line shows both figures; no warning is visible. Then flip `demo.solar_status()` to `"capped": True` temporarily and confirm the red warning appears. Restore it. Repeat in dark mode.

- [ ] **Step 9: Run the full suite and commit**

```bash
git add static/car.js static/car.html static/styles.css demo.py solar_routes.py tests/test_solar_routes.py
git commit -m "feat: solar status card with demo fixtures

The raised charge limit renders beside the original so a stuck raise is
visible rather than discovered a month later.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Backtest against real weather

Proves the controller behaves on 200 days of actual Colorado cloud rather than synthetic sawtooths.

**Files:**
- Create: `tools/backtest.py`, `tests/test_backtest.py`

**Interfaces:**
- Consumes: `solar.control`, `solar.advance`, `calendar_history?kind=energy&period=day`.
- Produces:
  - `backtest.grid_watts(bucket: dict) -> float` — converts a 5-minute energy bucket to average watts
  - `backtest.simulate(buckets: list[dict], cfg: dict) -> dict` — returns `{captured_kwh, imported_wh, stop_starts, ticks}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_backtest.py`:

```python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import backtest
import solar


def bucket(imported=0, exported_solar=0, solar_wh=0):
    return {"grid_energy_imported": imported,
            "grid_energy_exported_from_solar": exported_solar,
            "grid_energy_exported_from_battery": 0,
            "grid_energy_exported_from_generator": 0,
            "solar_energy_exported": solar_wh}


def test_energy_buckets_convert_to_average_watts():
    """calendar_history returns Wh per 5 minutes; the controller wants watts.
    500 Wh over 5 minutes is 6000 W."""
    assert backtest.grid_watts(bucket(imported=500)) == 6000.0
    assert backtest.grid_watts(bucket(exported_solar=500)) == -6000.0


def test_a_sunny_day_charges_without_importing():
    day = [bucket(exported_solar=800, solar_wh=1000) for _ in range(96)]
    out = backtest.simulate(day, {})
    assert out["imported_wh"] == 0
    assert out["captured_kwh"] > 0


def test_a_dark_day_never_starts():
    day = [bucket(imported=300) for _ in range(96)]
    out = backtest.simulate(day, {})
    assert out["captured_kwh"] == 0
    assert out["stop_starts"] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_backtest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backtest'`

- [ ] **Step 3: Write `tools/backtest.py`**

```python
"""Replay real metered history through the controller, offline.

WHAT THIS PROVES AND WHAT IT DOES NOT.

calendar_history?kind=energy returns watt-HOURS per 5-minute bucket; the
controller consumes instantaneous signed watts. Converting gives the AVERAGE
over each bucket, which smooths away sub-5-minute cloud transients. The backtest
therefore UNDERSTATES stop/start churn -- treat its cycle count as a floor.

Only days when the car was away are valid inputs, because the buckets contain
the car's own historical draw and replaying a day it charged double-counts it.
Known-clean days on this account: 2026-07-14, 2026-07-15, 2026-07-21.

Usage:
    .venv/bin/python tools/backtest.py 2026-07-21
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import solar
from config import settings
from tesla import TeslaClient

BUCKET_S = 300
CLEAN_DAYS = ("2026-07-14", "2026-07-15", "2026-07-21")


def grid_watts(b: dict) -> float:
    """One 5-minute energy bucket -> average signed watts. Positive is import."""
    net_wh = (float(b.get("grid_energy_imported") or 0)
              - float(b.get("grid_energy_exported_from_solar") or 0)
              - float(b.get("grid_energy_exported_from_battery") or 0)
              - float(b.get("grid_energy_exported_from_generator") or 0))
    return net_wh * 3600.0 / BUCKET_S


def simulate(buckets: list[dict], cfg: dict) -> dict:
    conf = dict(solar.CONFIG_DEFAULTS)
    conf.update(cfg or {})
    conf["enabled"] = 1
    tun = solar.tunables_from(conf, amps_max=48, volts=240)
    pol = solar.policy_from(conf)

    machine = solar.Machine(state="idle")
    amps = tun.min_a
    captured_wh = imported_wh = 0.0
    stop_starts = 0

    for b in buckets:
        house_grid_w = grid_watts(b)
        car_w = amps * tun.volts if machine.state in ("charging", "grace") else 0.0
        # The car's draw is added back, because the historical meter never saw it.
        grid_w = house_grid_w + car_w
        decision = solar.control(grid_w, amps, tun)
        tick = solar.Tick(
            surplus_w=solar.surplus_watts(car_w, grid_w), decision=decision,
            location="home", plugged=True, period_s=BUCKET_S)
        machine, actions = solar.advance(machine, tick, pol, tun)

        if "charge_stop" in actions:
            stop_starts += 1
        if "set_amps" in actions:
            amps = tun.min_a if machine.state == "grace" else decision.target_a
        if machine.state in ("charging", "grace"):
            drawn_wh = amps * tun.volts * BUCKET_S / 3600.0
            captured_wh += drawn_wh
            if grid_w > 0:
                imported_wh += grid_w * BUCKET_S / 3600.0

    return {"captured_kwh": round(captured_wh / 1000.0, 3),
            "imported_wh": round(imported_wh, 1),
            "stop_starts": stop_starts, "ticks": len(buckets)}


async def _fetch(day: str) -> list[dict]:
    tz = ZoneInfo(settings.timezone)
    end = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=tz)
    client = TeslaClient(settings)
    try:
        sites = await client.energy_sites()
        hist = await client._get(
            f"/api/1/energy_sites/{sites[0]['energy_site_id']}/calendar_history",
            params={"kind": "energy", "period": "day",
                    "end_date": end.isoformat(), "time_zone": settings.timezone},
            ttl=0)
        return (hist or {}).get("time_series") or []
    finally:
        await client.aclose()


def main() -> int:
    days = sys.argv[1:] or list(CLEAN_DAYS)
    for day in days:
        if day not in CLEAN_DAYS:
            print(f"WARNING {day} is not a known car-away day; "
                  "results include the car's own historical draw")
        buckets = asyncio.run(_fetch(day))
        if not buckets:
            print(f"{day}: no data")
            continue
        out = simulate(buckets, {})
        print(f"{day}: captured {out['captured_kwh']} kWh, "
              f"imported {out['imported_wh']} Wh, "
              f"{out['stop_starts']} stop/starts over {out['ticks']} buckets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_backtest.py -v`
Expected: 3 passed

- [ ] **Step 5: Run it against the three clean days**

```bash
.venv/bin/python tools/backtest.py
```

Expected: three lines. Sanity gates — investigate before proceeding if any fails:
- `captured_kwh` between 10 and 34 on each of these days (they exported 33.6, 33.2, and 33.2 kWh).
- `imported_wh` small — at most a few hundred Wh, all attributable to grace.
- `stop_starts` in single digits. If it is 20+, the dwell and hysteresis are not doing their job and Task 6 needs revisiting.

- [ ] **Step 6: Commit**

```bash
git add tools/backtest.py tests/test_backtest.py
git commit -m "test: backtest the controller against real metered history

Replays 5-minute energy buckets from calendar_history. The conversion yields
per-bucket averages, so sub-5-minute transients are smoothed and the reported
stop/start count is a floor, not an estimate.

Only car-away days are valid inputs; other days contain the car's own draw.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: End-to-end verification on the real car

Nothing before this proves the loop works on hardware. Run this with the car plugged in at home on a sunny day.

**Files:** none — this is verification.

- [ ] **Step 1: Confirm the collector is running and clean**

```bash
launchctl list | grep tesla-collector
tail -20 /Users/d/Code/tesla_automation/collector.log
```

Expected: exit status `0`, recent state lines, no traceback.

- [ ] **Step 2: Set home from the setup page**

Open `http://localhost:8000/setup.html`, drag the pin to where the car parks, save, and confirm the classification reads `home` while the car is actually there. **If it reads `away` or `unknown`, stop** — the geofence is wrong and enabling the controller would do nothing useful.

- [ ] **Step 3: Record the car's current settings**

```bash
curl -s localhost:8000/api/car/state | .venv/bin/python -c "
import json,sys; v=json.load(sys.stdin).get('view') or {}
print('amps', v.get('charge_amps'), 'limit', v.get('limit'), 'state', v.get('charging_state'))"
```

Write these down. They are what the loop must restore.

- [ ] **Step 4: Enable with a conservative configuration**

On the setup page: enable, period 120 s, grace 3 min, ceiling at your current limit (so the raise cannot fire on this first run), request cap 300.

- [ ] **Step 5: Watch three ticks**

```bash
tail -f /Users/d/Code/tesla_automation/collector.log
```

Expected within ~6 minutes: three `solar <state> surplus=<n>W amps=<a>-><b>` lines. Confirm:
- `surplus` is positive and plausible against the solar page's current export.
- Amps move toward matching it rather than oscillating between extremes.
- The state is `charging`, not flapping into `grace` — **if it enters `grace` while the sun is clearly out, stop and re-check Task 6.**

- [ ] **Step 6: Confirm the meter actually goes to zero**

```bash
curl -s "localhost:8000/api/dashboard?period=today" | .venv/bin/python -c "
import json,sys; l=json.load(sys.stdin)['live']
print('grid', l['grid'], 'kW', l['grid_direction'], '| solar', l['solar'], '| home', l['home'])"
```

Expected after several minutes: `grid_direction` is `export` or `idle` with a small magnitude. Sustained `import` above ~0.3 kW means the loop is not keeping up — check `margin_w` and the ramp limit.

- [ ] **Step 7: Verify restoration**

Disable the controller on the setup page. Within one tick:

```bash
curl -s localhost:8000/api/car/solar/status | .venv/bin/python -m json.tool | grep -E "state|dirty"
curl -s localhost:8000/api/car/state | .venv/bin/python -c "
import json,sys; v=json.load(sys.stdin).get('view') or {}
print('amps', v.get('charge_amps'), 'limit', v.get('limit'))"
```

Expected: `state` is `idle`, `dirty` is `false`, and amps and limit match Step 3 exactly. **This is the most important check in the plan** — the car remembers amps per location, so a failed restore silently degrades every future overnight charge.

- [ ] **Step 8: Record the real cost**

After 24 hours with the controller enabled, check the Tesla developer dashboard for actual spend, and:

```bash
curl -s localhost:8000/api/car/solar/status | .venv/bin/python -c "
import json,sys; d=json.load(sys.stdin); print('requests today:', d['requests_today'])"
```

Compare against the spec §1.7 estimate of ~360 data calls per active day and tune `period_s` and `daily_request_cap` accordingly. Update spec §8 item 1 with the measured figure.

- [ ] **Step 9: Update the ledger**

Append the outcome to `.superpowers/sdd/` progress notes: measured request count, observed stop/start rate, whether restoration worked first time, and any tunable you changed from its default.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §1.7 correct the "FREE" comment | 1 Step 8 |
| §2.1 one background process | 9 |
| §2.2 module boundaries | 4, 5, 6, 7, 8 |
| §2.3 three-valued classification | 4 |
| §2.4 cross-process token refresh | 1 |
| §3.1 disjoint symbols | 5 |
| §3.2 control law, integer target | 5 |
| §3.3 state machine, dwell, hysteresis, `charge_start`, ramp exception | 6 |
| §3.4 charge-limit raise | 9 (`raise_limit`, `raised_to`, ceiling); UI in 10 |
| §3.5 request cap | 7, 9 |
| §3.6 crash safety and the three restore gates | 6 (`may_restore`), 9 |
| §3.7 invariants | 5, 6, 9 |
| §4 schema | 3, 4, 7 |
| §4.1 migration + writer + view model | 3 |
| §4.2 upstream fixes | 3 |
| §5 failure table | 9 |
| §6.1 pure-function tables | 5, 6 |
| §6.2 backtest + adapter | 12 |
| §6.3 token refresh test | 1 |
| §6.6 demo mode | 11 |
| §6.7 browser verification | 10 Step 5, 11 Step 8 |
| §7.1 setup page | 10 |
| §7.2 car-page card | 11 |
| §7.4 honesty rules | 11 |
| §8 live verification | 13 |

**Gap found and closed:** §7.3's deadline warning has config storage (Task 7), API bounds and a UI (Tasks 8, 10) but no evaluator. It is deliberately deferred — it is a display-only feature with no effect on control, it needs a full solar day of `solar_ticks` to test against, and Task 12's backtest produces exactly that corpus. **Tracked as the first item of spec 3's plan**, which is where the tick history it reads becomes available.

**Placeholder scan:** no TBDs, no "add error handling", no "similar to Task N". Every code step carries the actual code.

**Type consistency:** `classify(view, cfg)` matches between `home.py` and every caller. `Decision`, `Machine`, `Tick`, `Tunables`, `Policy` field names are identical in Tasks 5, 6, 9 and 12. `store.record(view, at_home=None)` matches its Task 9 caller. `next_interval(..., solar_engaged: int)` is an int (the period in seconds), not a bool, in both the definition and both call sites.

**One defect found and fixed during this review:** the first draft persisted only `state`, not the machine's counters. Since each tick is a separate pass that reloads from the database, `breach_ticks` would have reset to zero every time — so the two-tick dwell could never reach two, and the state machine would have flapped into `grace` on the first ambiguous reading despite the tests in Task 6 passing (they hold the machine in memory across calls, which the real loop does not). All five `Machine` fields are now columns in `solar_state`, with `machine_from()` / `machine_fields()` as the single conversion point and a `dataclasses.fields` test that fails if anyone adds a sixth counter without persisting it.

This is worth noting for whoever executes the plan: **passing unit tests would not have caught this.** The gap was between the pure function's contract and the way the caller reconstructs its input.

---

Plan complete.

---

## Task 14: The dynamic charge-limit raise (spec §3.4)

Added after the final whole-branch review confirmed §3.4 was never implemented.
Config, storage column, setup-page slider and restore-of-a-raise all exist;
nothing performs a raise.

**Why it is most of the feature, not a refinement.** At this owner's 80% limit
with the car at 76%, there are ~4 kWh of SoC headroom against a median
13.8 kWh/day of export. Without the raise the controller fills the pack in
under two hours and watches the rest go to the grid — roughly 29% of a median
day. Raising to 90% covers a full median day.

**Files:**
- Modify: `solar.py` (pure `raise_decision()`, one new state column)
- Modify: `collector.py` (call it in `solar_tick`; reset the timer on disengage)
- Test: `tests/test_solar_raise.py` (create), `tests/test_collector_solar.py` (extend)

**Interfaces:**
- Produces `solar.raise_decision(**kwargs) -> tuple[int | None, int]` returning
  `(limit_to_raise_to_or_None, new_hold_elapsed_s)`.
- Adds `raise_hold_elapsed` to `STATE_DEFAULTS` and the `solar_state` DDL.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_solar_raise.py`:

```python
from __future__ import annotations

import solar

BASE = dict(enabled=True, state="charging", soc=79, limit=80, ceiling=90,
            grid_w=-3000.0, raised_to=None, hold_elapsed_s=600,
            raise_hold_s=600, period_s=120)


def d(**over):
    kw = dict(BASE); kw.update(over)
    return solar.raise_decision(**kw)


def test_raises_when_every_condition_holds():
    target, hold = d()
    assert target == 90


def test_never_raises_when_the_feature_is_off():
    assert d(enabled=False)[0] is None


def test_never_raises_outside_charging():
    for state in ("idle", "grace", "stopped"):
        assert d(state=state)[0] is None, state


def test_raises_at_most_once_per_engagement():
    """raised_to is the guard. Re-issuing set_charge_limit every tick would
    spend a billed command to assert a value the car already holds."""
    assert d(raised_to=90)[0] is None


def test_never_raises_speculatively():
    """Only while surplus is ACTUALLY being exported. grid_w >= 0 means the
    house is importing or balanced -- there is nothing to absorb."""
    assert d(grid_w=0.0)[0] is None
    assert d(grid_w=500.0)[0] is None


def test_does_not_raise_while_headroom_remains():
    """Below limit-2 there is still room to charge into; raising early would
    park the pack high for longer than necessary, which is what ages it."""
    assert d(soc=60)[0] is None


def test_unknown_soc_or_limit_never_raises():
    assert d(soc=None)[0] is None
    assert d(limit=None)[0] is None


def test_requires_the_hold_to_have_elapsed_on_a_previous_tick():
    """Same whole-tick semantics as start_hold_s: the timer is compared as
    carried in, so a period coarser than the hold still takes two ticks."""
    target, hold = d(hold_elapsed_s=0)
    assert target is None
    assert hold == 120


def test_the_timer_resets_when_a_condition_lapses():
    assert d(hold_elapsed_s=480, grid_w=+200.0)[1] == 0


def test_never_raises_below_or_equal_to_the_current_limit():
    assert d(ceiling=80)[0] is None
    assert d(ceiling=70)[0] is None


def test_the_ceiling_is_capped_at_one_hundred():
    assert d(ceiling=110, limit=95, soc=94)[0] == 100
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_solar_raise.py -v`
Expected: FAIL, `AttributeError: module 'solar' has no attribute 'raise_decision'`

- [ ] **Step 3: Implement the pure decision**

Append to `solar.py`, beside the other pure functions:

```python
def raise_decision(*, enabled: bool, state: str, soc: int | None,
                   limit: int | None, ceiling: int, grid_w: float,
                   raised_to: int | None, hold_elapsed_s: int,
                   raise_hold_s: int, period_s: int) -> tuple[int | None, int]:
    """Whether to raise the charge limit, and the updated hold timer.

    Returns (limit_to_raise_to or None, new_hold_elapsed_s).

    Headroom, not surplus, is the binding constraint on this system: at an 80%
    limit with the car at 76% there are ~4 kWh of room against a median
    13.8 kWh/day of export, so without a raise the controller fills the pack in
    under two hours and the rest goes to the grid anyway.

    Every gate here exists for a reason:
      * ACTUALLY EXPORTING (grid_w < 0), never speculatively -- raising the
        limit on a forecast would park an NCA pack high for nothing.
      * NEAR THE LIMIT (soc >= limit - 2) -- while headroom remains there is
        somewhere to put the energy already, and time spent high is what ages
        the pack, not reaching high.
      * ONCE PER ENGAGEMENT (raised_to is None) -- re-issuing the command every
        tick would spend a billed write to assert a value the car holds.
      * UNKNOWN NEVER GUESSES -- a missing soc or limit returns None, matching
        the three-valued discipline used for location.

    The hold is compared AS CARRIED IN, like start_hold_s, so the real wait is
    period_s * (ceil(raise_hold_s / period_s) + 1) -- never under two ticks.
    """
    if not enabled or state != "charging" or raised_to is not None:
        return None, 0
    if soc is None or limit is None:
        return None, 0
    if grid_w >= 0 or soc < limit - 2:
        return None, 0
    if hold_elapsed_s >= raise_hold_s:
        target = min(int(ceiling), 100)
        return (target if target > limit else None), hold_elapsed_s
    return None, hold_elapsed_s + period_s
```

Add `"raise_hold_elapsed": 0` to `STATE_DEFAULTS`, and
`raise_hold_elapsed INTEGER NOT NULL DEFAULT 0,` to the `solar_state` DDL.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_solar_raise.py -v`
Expected: 11 passed

- [ ] **Step 5: Wire it into the tick**

In `collector.py`'s `solar_tick`, after the action loop and before the
`save_state`/`log_tick` calls:

```python
    # --- charge-limit raise (spec 3.4) -------------------------------------
    target_limit, raise_hold = solar.raise_decision(
        enabled=bool(conf["raise_limit"]),
        state=machine.state,
        soc=view.get("soc"),
        limit=view.get("limit"),
        ceiling=conf["soc_ceiling"],
        grid_w=grid_w,
        raised_to=st["raised_to"],
        hold_elapsed_s=st["raise_hold_elapsed"],
        raise_hold_s=conf["raise_hold_s"],
        period_s=conf["period_s"],
    )
    raised = st["raised_to"]
    if target_limit is not None:
        if await _command(client, vin, "set_charge_limit", percent=target_limit):
            raised = target_limit
            _log(f"raised charge limit {view.get('limit')} -> {target_limit} "
                 f"for solar")
    solar.save_state(db, vin, raised_to=raised, raise_hold_elapsed=raise_hold)
```

The revert needs no new code: `_restore` already puts `original_limit` back,
gated on `raised_to`, and every disengage path calls it.

- [ ] **Step 6: Add the integration test**

Append to `tests/test_collector_solar.py` a multi-tick test driving the real
`solar_tick` with the car near its limit and steady export, asserting
`set_charge_limit` is issued **exactly once** across ≥ 8 ticks, and that
`solar_state.raised_to` holds the ceiling afterwards. Prove it discriminates by
removing the `raised_to is not None` guard and confirming the count rises.

- [ ] **Step 7: Run the full suite and commit**

```bash
git add solar.py collector.py tests/test_solar_raise.py tests/test_collector_solar.py
git commit -m "feat: dynamic charge-limit raise to absorb surplus solar

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```
