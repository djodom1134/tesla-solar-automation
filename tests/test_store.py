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


# --- Round-1 review fixes: rank-based bucketing must hold two invariants
# for ANY spacing, not just the uniformly-spaced fixtures above: never
# return more than `buckets` rows, and never drop a sample when the
# matching count is <= `buckets`. Time-width bucketing violated both.


@pytest.mark.parametrize("buckets", [2, 3, 4, 5, 6])
def test_history_never_exceeds_bucket_budget_on_exact_divisors(db, buckets):
    """Regression: when the data span divided evenly by `buckets`, the old
    time-width bucketing produced one bucket index past the intended
    0..buckets-1 range, returning buckets+1 rows. Reproduced against the
    1000-sample/60s-apart fixture varying only `buckets` (59940 % buckets
    happens to be 0 for each value here)."""
    for i in range(1000):
        db.record(view(1000 + i * 60, 50))
    rows = db.history("VIN1", 0, 10 ** 9, buckets=buckets)
    assert len(rows) == buckets


def test_history_keeps_all_samples_when_count_is_under_budget_and_unevenly_spaced(db):
    """Regression: two tightly-clustered samples plus one far-off outlier,
    well under the bucket budget, used to collapse into one bucket under
    time-width bucketing (both clustered samples share one wide bucket) even
    though there are far fewer samples than buckets. Every sample must
    survive whenever count <= buckets."""
    db.record(view(1000, 50))
    db.record(view(1100, 51))
    db.record(view(101000, 52))
    rows = db.history("VIN1", 0, 10 ** 9, buckets=10)
    assert [r["ts"] for r in rows] == [1000, 1100, 101000]


def test_gap_boundary_is_exclusive(db):
    """A delta of exactly GAP_SECONDS is still adjacent, not a gap -- only
    strictly further apart counts as a sleep hole. Pins the `>` in the gap
    comparison against an accidental switch to `>=`."""
    db.record(view(1000, 80))
    db.record(view(1000 + store.GAP_SECONDS, 78))
    rows = db.history("VIN1", 0, 10 ** 9)
    assert [r["gap"] for r in rows] == [False, False]


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


def test_solar_state_migration_adds_raise_hold_elapsed_to_an_existing_table(tmp_path):
    """The owner's live car.db already has a solar_state table from before
    task 14 -- CREATE TABLE IF NOT EXISTS is a no-op against it, so opening
    Store must ALTER TABLE the new column in rather than crash the next
    load_state() call, and existing rows must survive untouched."""
    import sqlite3
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE solar_state (
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
    """)
    legacy.execute(
        "INSERT INTO solar_state (vin, state, raised_to, updated_at) "
        "VALUES ('VIN1', 'charging', 90, 0)")
    legacy.commit()
    legacy.close()

    from store import Store
    import solar
    s = Store(path)
    cols = {r[1] for r in s._db.execute("PRAGMA table_info(solar_state)")}
    assert "raise_hold_elapsed" in cols, "missing after migration"

    st = solar.load_state(s._db, "VIN1")
    assert st["raised_to"] == 90, "pre-existing data must survive the migration"
    assert st["raise_hold_elapsed"] == 0, "new column must default cleanly"
    s.close()


def test_solar_state_migration_adds_the_429_backoff_columns_to_an_existing_table(tmp_path):
    """Same hazard as the raise_hold_elapsed case just above, for the two
    columns invariant 4 (spec 3.7) adds: consecutive_429s and backoff_s.
    Without migrate_state ALTERing them onto the live table, load_state()'s
    `{k: row[k] for k in STATE_DEFAULTS}` raises IndexError on the very first
    tick after this ships -- exactly the class of defect the standing
    instruction calls out as already caught once."""
    import sqlite3
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE solar_state (
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
          raise_hold_elapsed INTEGER NOT NULL DEFAULT 0,
          requests_today  INTEGER NOT NULL DEFAULT 0,
          requests_day    TEXT,
          capped          INTEGER NOT NULL DEFAULT 0,
          engaged_at      INTEGER,
          updated_at      INTEGER NOT NULL DEFAULT 0
        );
    """)
    legacy.execute(
        "INSERT INTO solar_state (vin, state, raised_to, updated_at) "
        "VALUES ('VIN1', 'charging', 90, 0)")
    legacy.commit()
    legacy.close()

    from store import Store
    import solar
    s = Store(path)
    cols = {r[1] for r in s._db.execute("PRAGMA table_info(solar_state)")}
    assert "consecutive_429s" in cols, "missing after migration"
    assert "backoff_s" in cols, "missing after migration"

    st = solar.load_state(s._db, "VIN1")
    assert st["raised_to"] == 90, "pre-existing data must survive the migration"
    assert st["consecutive_429s"] == 0, "new column must default cleanly"
    assert st["backoff_s"] == 0, "new column must default cleanly"
    s.close()


def test_solar_state_migration_adds_the_garage_columns_to_an_existing_table(tmp_path):
    """Task 17b's garage_armed / garage_last_close_day, added to solar_state
    exactly like raise_hold_elapsed and the 429 columns above. Without
    migrate_state ALTERing them onto the live table, load_state() crashes on
    the first tick after this ships -- the standing instruction's exact
    example of an already-caught defect."""
    import sqlite3
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE solar_state (
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
          raise_hold_elapsed INTEGER NOT NULL DEFAULT 0,
          requests_today  INTEGER NOT NULL DEFAULT 0,
          requests_day    TEXT,
          capped          INTEGER NOT NULL DEFAULT 0,
          engaged_at      INTEGER,
          consecutive_429s INTEGER NOT NULL DEFAULT 0,
          backoff_s        INTEGER NOT NULL DEFAULT 0,
          updated_at      INTEGER NOT NULL DEFAULT 0
        );
    """)
    legacy.execute(
        "INSERT INTO solar_state (vin, state, raised_to, updated_at) "
        "VALUES ('VIN1', 'charging', 90, 0)")
    legacy.commit()
    legacy.close()

    from store import Store
    import solar
    s = Store(path)
    cols = {r[1] for r in s._db.execute("PRAGMA table_info(solar_state)")}
    assert "garage_armed" in cols, "missing after migration"
    assert "garage_last_close_day" in cols, "missing after migration"

    st = solar.load_state(s._db, "VIN1")
    assert st["raised_to"] == 90, "pre-existing data must survive the migration"
    assert st["garage_armed"] == 0, "new column must default cleanly"
    assert st["garage_last_close_day"] is None, "new column must default cleanly"
    s.close()


def test_solar_state_migration_adds_the_banked_solar_ledger_columns(tmp_path):
    """Task 18's solar_soc / ledger_soc / ledger_stale, added to solar_state
    exactly like every prior column above. This is the owner's actual live
    car.db shape today -- a row with a real engagement already on it, from
    before this feature existed. Without migrate_state ALTERing the three
    new columns onto the live table, load_state() raises IndexError on the
    very first tick after this ships -- the standing instruction's exact
    example of an already-caught defect, and the one this test exists to
    make sure it never comes back."""
    import sqlite3
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE solar_state (
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
          raise_hold_elapsed INTEGER NOT NULL DEFAULT 0,
          requests_today  INTEGER NOT NULL DEFAULT 0,
          requests_day    TEXT,
          capped          INTEGER NOT NULL DEFAULT 0,
          engaged_at      INTEGER,
          consecutive_429s INTEGER NOT NULL DEFAULT 0,
          backoff_s        INTEGER NOT NULL DEFAULT 0,
          garage_armed          INTEGER NOT NULL DEFAULT 0,
          garage_last_close_day TEXT,
          updated_at      INTEGER NOT NULL DEFAULT 0
        );
    """)
    legacy.execute(
        "INSERT INTO solar_state (vin, state, raised_to, updated_at) "
        "VALUES ('VIN1', 'charging', 90, 0)")
    legacy.commit()
    legacy.close()

    from store import Store
    import solar
    s = Store(path)
    cols = {r[1] for r in s._db.execute("PRAGMA table_info(solar_state)")}
    assert "solar_soc" in cols, "missing after migration"
    assert "ledger_soc" in cols, "missing after migration"
    assert "ledger_stale" in cols, "missing after migration"

    st = solar.load_state(s._db, "VIN1")
    assert st["raised_to"] == 90, "pre-existing data must survive the migration"
    assert st["solar_soc"] == 0, "new column must default cleanly, not from a guess"
    assert st["ledger_soc"] is None, "no previous observation yet -- must not assume one"
    assert st["ledger_stale"] == 0, "new column must default cleanly"
    s.close()


def test_solar_state_migration_adds_the_free_miles_ledger_columns(tmp_path):
    """Task 20's free_miles_driven / tracked_miles / ledger_odo /
    free_miles_since, added to solar_state exactly like every prior column
    above. The legacy shape here is the owner's ACTUAL live car.db today --
    solar_soc/ledger_soc/ledger_stale (Task 18) already present, the four
    Task 20 columns not yet. Without migrate_state ALTERing them onto the
    live table, load_state() raises IndexError on the very first tick after
    this ships -- the same defect class the standing instruction calls out,
    and the one this test exists to make sure never comes back."""
    import sqlite3
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE solar_state (
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
          raise_hold_elapsed INTEGER NOT NULL DEFAULT 0,
          requests_today  INTEGER NOT NULL DEFAULT 0,
          requests_day    TEXT,
          capped          INTEGER NOT NULL DEFAULT 0,
          engaged_at      INTEGER,
          consecutive_429s INTEGER NOT NULL DEFAULT 0,
          backoff_s        INTEGER NOT NULL DEFAULT 0,
          garage_armed          INTEGER NOT NULL DEFAULT 0,
          garage_last_close_day TEXT,
          solar_soc       REAL    NOT NULL DEFAULT 0,
          ledger_soc      INTEGER,
          ledger_stale    INTEGER NOT NULL DEFAULT 0,
          updated_at      INTEGER NOT NULL DEFAULT 0
        );
    """)
    legacy.execute(
        "INSERT INTO solar_state (vin, state, raised_to, solar_soc, ledger_soc, updated_at) "
        "VALUES ('VIN1', 'idle', NULL, 0.0, 78, 0)")
    legacy.commit()
    legacy.close()

    from store import Store
    import solar
    s = Store(path)
    cols = {r[1] for r in s._db.execute("PRAGMA table_info(solar_state)")}
    assert "free_miles_driven" in cols, "missing after migration"
    assert "tracked_miles" in cols, "missing after migration"
    assert "ledger_odo" in cols, "missing after migration"
    assert "free_miles_since" in cols, "missing after migration"

    st = solar.load_state(s._db, "VIN1")
    assert st["solar_soc"] == 0.0, "pre-existing Task 18 data must survive the migration"
    assert st["ledger_soc"] == 78, "pre-existing Task 18 data must survive the migration"
    assert st["free_miles_driven"] == 0, "new column must default cleanly, not from a guess"
    assert st["tracked_miles"] == 0, "new column must default cleanly"
    assert st["ledger_odo"] is None, "no previous odometer yet -- must not assume one"
    assert st["free_miles_since"] is None, "never tracked yet -- must not invent a start date"
    s.close()


def test_solar_config_migration_adds_the_garage_columns_to_an_existing_table(tmp_path):
    """The owner's live car.db already has a solar_config table with rows in
    it from before task 17b -- CREATE TABLE IF NOT EXISTS is a no-op against
    it. This is the first-ever migration needed for solar_config (deadline_soc
    and deadline_hour shipped in the original schema), so this is also the
    first test proving migrate_config() actually runs from Store.__init__."""
    import sqlite3
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE solar_config (
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
          daily_request_cap INTEGER NOT NULL DEFAULT 400,
          view_refresh_ticks INTEGER NOT NULL DEFAULT 5,
          deadline_soc      INTEGER,
          deadline_hour     INTEGER,
          updated_at        INTEGER NOT NULL DEFAULT 0
        );
    """)
    legacy.execute(
        "INSERT INTO solar_config (id, enabled, soc_ceiling, updated_at) "
        "VALUES (1, 1, 95, 0)")
    legacy.commit()
    legacy.close()

    from store import Store
    import solar
    s = Store(path)
    cols = {r[1] for r in s._db.execute("PRAGMA table_info(solar_config)")}
    for name in ("garage_url", "garage_auto_open", "garage_ring_m",
                 "garage_close_hour", "garage_close_warn_s"):
        assert name in cols, f"{name} missing after migration"

    cfg = solar.load_config(s._db)
    assert cfg["enabled"] == 1 and cfg["soc_ceiling"] == 95, (
        "pre-existing data must survive the migration")
    assert cfg["garage_url"] is None, "new column must default cleanly"
    assert cfg["garage_auto_open"] == 0
    assert cfg["garage_ring_m"] == 800
    assert cfg["garage_close_hour"] is None
    assert cfg["garage_close_warn_s"] == 8
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


def test_latest_vin_answers_which_car_without_touching_the_api(tmp_path):
    """Exists so a route can answer "which car is this?" for free.

    car_routes._vin() used to call resolve_vin() on every request, which with
    TESLA_VIN unset falls through to a billed GET /api/1/vehicles -- paid even
    by /history, which otherwise reads nothing but this table.
    """
    st = store.Store(tmp_path / "car.db")
    assert st.latest_vin() is None, "nothing recorded yet is None, not a guess"

    st.record({"vin": "VIN_OLD", "soc": 50, "sampled_at": 100,
               "charging_state": "Stopped", "amps_actual": 0, "charging": 0},
              at_home=True)
    assert st.latest_vin() == "VIN_OLD"

    # Most RECENT wins -- a car swapped in is the one the routes should address.
    st.record({"vin": "VIN_NEW", "soc": 60, "sampled_at": 200,
               "charging_state": "Stopped", "amps_actual": 0, "charging": 0},
              at_home=True)
    assert st.latest_vin() == "VIN_NEW"
    st.close()


def test_correct_snapshot_fixes_fields_without_freshening_the_view(db):
    """2026-09-17. A refusal proved the stored view's plug wrong (see
    solar.snapshot_correction). Writing the correction must NOT move `ts`:
    the view is now less wrong, not newer, and snapshot_age_s is the one
    signal that was telling the truth throughout the outage -- 160438 s,
    right there in /api/ha/state, while every flag beside it said fine.
    """
    db.record(view(1000, 39))
    db._db.execute("UPDATE snapshot SET json = json_set(json, '$.plugged_in', 1)")
    db._db.execute(
        "UPDATE snapshot SET json = json_set(json, '$.charging_state', 'Stopped')")

    assert store.correct_snapshot(
        db._db, "VIN1", {"charging_state": "Disconnected", "plugged_in": False})

    snap = db.snapshot("VIN1")
    assert snap["view"]["charging_state"] == "Disconnected"
    assert snap["view"]["plugged_in"] is False
    assert snap["ts"] == 1000, "a correction is not an observation"
    assert snap["view"]["soc"] == 39, "untouched fields survive"


def test_correct_snapshot_is_a_no_op_with_nothing_to_correct(db):
    """No snapshot yet, or the view already agrees. Returns False so the
    caller can log a real correction and stay quiet about a redundant one."""
    assert not store.correct_snapshot(db._db, "NOSUCH", {"plugged_in": False})
    db.record(view(1000, 39))          # records charging_state Disconnected
    assert not store.correct_snapshot(
        db._db, "VIN1", {"charging_state": "Disconnected"})
