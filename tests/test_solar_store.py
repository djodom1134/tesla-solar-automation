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


def test_config_updates_survive_each_other():
    """The load-bearing guarantee: a second save touching a DIFFERENT field
    must not revert the first save's field to its default. A writer that
    rebuilt from CONFIG_DEFAULTS every call would pass every other test in
    this file."""
    conn = db()
    solar.save_config(conn, enabled=1)
    solar.save_config(conn, soc_ceiling=100)
    cfg = solar.load_config(conn)
    assert cfg["enabled"] == 1, "second save reverted the first"
    assert cfg["soc_ceiling"] == 100
    assert cfg["period_s"] == 120, "untouched field should still be default"


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


def test_state_updates_survive_each_other():
    conn = db()
    solar.save_state(conn, "V1", dirty=1)
    solar.save_state(conn, "V1", state="charging")
    st = solar.load_state(conn, "V1")
    assert st["dirty"] == 1, "second save reverted the first"
    assert st["state"] == "charging"


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
