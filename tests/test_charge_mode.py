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
