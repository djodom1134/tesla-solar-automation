"""Closed-loop solar charge control.

The control law and the state machine are pure: they take numbers and return
numbers, with no I/O of their own. collector.py owns the network and drives
them tick by tick. That split is what makes a control system testable without
a car, a roof, or the weather.

The persistence functions below them are the deliberate exception: they are
where the control law's and state machine's numbers live between ticks --
config, machine state, tick history, and the request counter -- and they do
touch a database. Nothing above them in this file does.

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

import sqlite3
import time
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
    # Clamped to [min_a, max_a] but NOT ramp-limited -- the "where the loop
    # would put the car right now, with no regard for ramp_a" figure. Used
    # only by the adoption-tick bypass in collector.py: a downward move all
    # the way there is safe (it can only reduce draw), an upward one is not,
    # so the caller still compares it against current_a before trusting it.
    unramped_target_a: int


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


# Spec 5: "grid_power stuck | same value > 5 consecutive ticks | hold amps,
# flag suspect". Six consecutive byte-identical readings (">5") is the trigger;
# named here so collector.py's history fetch and this check can never drift
# out of sync with each other.
GRID_STUCK_TICKS = 6


def grid_is_stuck(recent: list[float], threshold: int = GRID_STUCK_TICKS) -> bool:
    """True when the grid meter has reported the SAME value too many ticks running.

    The meter refreshes every 60 s against a 120 s loop, so one or two repeats
    are normal and expected. A long run of byte-identical readings is not: it
    means the gateway has frozen, and a frozen NEGATIVE reading is the dangerous
    one -- error_w stays positive, the controller ramps to max_a and holds, and
    nothing in the state machine can ever observe the floor breach that would
    stop it. It would draw full power from the grid all night while the meter
    insisted it was sunny.

    Compares exact equality deliberately: a live meter essentially never repeats
    a float bit-for-bit, and any tolerance would mask a genuinely frozen value
    that happens to sit near a real one.
    """
    if len(recent) < threshold:
        return False
    return all(v == recent[0] for v in recent[:threshold])


def control(grid_w: float, current_a: int, tun: Tunables) -> Decision:
    """One tick of the integral controller."""
    error_w = -grid_w - tun.margin_w
    raw_target = current_a + error_w / tun.volts
    step_a = int(round(_clamp(error_w / tun.volts, -tun.ramp_a, tun.ramp_a)))
    target_a = int(_clamp(current_a + step_a, tun.min_a, tun.max_a))
    unramped_target_a = int(_clamp(round(raw_target), tun.min_a, tun.max_a))
    return Decision(
        target_a=target_a,
        write=abs(error_w) >= tun.deadband_w,
        # Expressed in AMPS, not watts, so the floor derives from the measured
        # voltage instead of a hardcoded 1200 W -- and so it stays correct in
        # every state rather than only when the car is idle.
        floor_breach=raw_target < tun.min_a,
        error_w=error_w,
        raw_target=raw_target,
        unramped_target_a=unramped_target_a,
    )


def backoff_seconds(consecutive_429s: int, period_s: int,
                    retry_after: float | None, cap_s: int = 1800) -> int:
    """How long to wait after a 429 before the next request.

    Rate limits are shared with every other app on the owner's Tesla account,
    and exceeding Tesla's limit disables the whole application rather than
    merely throttling it -- so backing off is protecting access, not politeness.

    Honours a server-supplied Retry-After when there is one; the server knows
    better than any heuristic. Otherwise doubles from the configured period and
    caps, so a sustained outage settles at a slow poll rather than compounding.
    """
    if retry_after is not None:
        return min(int(round(retry_after)), cap_s)
    return min(period_s * 2 ** max(consecutive_429s, 0), cap_s)


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


STATES = frozenset({"idle", "charging", "grace", "stopped"})


@dataclass(frozen=True)
class Policy:
    grace_s: int = 180          # hold at min_a this long before giving up
    # start_hold_s and restart_hold_s are enforced as WHOLE TICKS, not raw
    # seconds: the timer is compared as carried in from the previous tick, so
    # the real wait is period_s * (ceil(threshold / period_s) + 1) and is never
    # less than two ticks. At the default 120 s tick, start_hold_s=60 means a
    # 240 s wait, and restart_hold_s=300 means 480 s. At a 300 s tick they
    # become 600 s and 600 s. This is deliberate -- one grid-meter reading is
    # not evidence of a *sustained* condition, and both transitions issue
    # billed commands -- but the numbers are not the wait in seconds.
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
    # True when the car is ALREADY drawing power we did not command -- it
    # auto-started on plug-in, or was started from the Tesla app. Defaults
    # False so every pre-existing call site (none of which knows about
    # adoption) keeps behaving exactly as before.
    car_charging: bool = False


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
        if t.car_charging:
            # ADOPT: the car is already charging -- it auto-started on
            # plug-in, or the owner started it from the app. No new
            # `charge_start` (it would be commanding a charge that is
            # already running) and NO sustained hold: the hold exists so one
            # noisy meter reading cannot START a charge, but here the charge
            # is already running and the only question is who controls it.
            # Waiting two ticks just means two more ticks of grid import.
            return Machine(state="charging"), ["adopt", "set_amps"]
        if t.surplus_w < start_watts(tun):
            return Machine(state="idle", hold_s=0), []
        # Threshold is checked against the timer *as carried in*, not the
        # value after this tick's period is folded in. A period longer than
        # start_hold_s must still take two ticks to fire, or "sustained"
        # would mean nothing when the tick is coarser than the hold.
        if m.hold_s >= pol.start_hold_s:
            return Machine(state="charging"), ["charge_start", "set_amps"]
        return Machine(state="idle", hold_s=m.hold_s + t.period_s), []

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
    if t.surplus_w < start_watts(tun):
        return Machine(state="stopped", hold_s=0), []
    if m.hold_s >= pol.restart_hold_s:
        return Machine(state="charging"), ["wake", "charge_start", "set_amps"]
    return Machine(state="stopped", hold_s=m.hold_s + t.period_s), []


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
  -- Task 17b: the ratgdo garage opener. garage_close_hour is NULL = off, the
  -- same "absence means disabled" convention as deadline_hour above.
  garage_url          TEXT,
  garage_auto_open    INTEGER NOT NULL DEFAULT 0,
  garage_ring_m       INTEGER NOT NULL DEFAULT 800,
  garage_close_hour   INTEGER,
  garage_close_warn_s INTEGER NOT NULL DEFAULT 8,
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
  raise_hold_elapsed INTEGER NOT NULL DEFAULT 0,
  requests_today  INTEGER NOT NULL DEFAULT 0,
  requests_day    TEXT,
  capped          INTEGER NOT NULL DEFAULT 0,
  engaged_at      INTEGER,
  -- Invariant 4 (spec 3.7): consecutive 429s from live_status, and the
  -- backoff currently in force because of them. Persisted, not just held in
  -- memory, so a restart mid-rate-limit-event does not resume hammering at
  -- the normal cadence -- see migrate_state() below, which is what actually
  -- gets these two columns onto the live table.
  consecutive_429s INTEGER NOT NULL DEFAULT 0,
  backoff_s        INTEGER NOT NULL DEFAULT 0,
  -- Task 17b: the garage arrival latch (armed on leaving the ring, fired --
  -- and disarmed -- at most once per re-entry) and the once-per-day stamp
  -- for the scheduled close, same convention as requests_day above.
  garage_armed          INTEGER NOT NULL DEFAULT 0,
  garage_last_close_day TEXT,
  -- Task 18: the banked-solar ledger. solar_soc is percentage points of the
  -- CURRENT soc that came from the sun (see green.ledger_step) -- a stock,
  -- not a flow, tracked in SoC space so it needs no pack size and no
  -- consumption figure. ledger_soc is the SoC as of the last observation,
  -- needed to diff consecutive samples; NULL until the ledger has observed
  -- its first tick, which is also how it starts at 0 rather than a guess.
  -- ledger_stale marks a lower bound: a gap long enough, AND crossed by a
  -- SoC change, that the pack may have moved unobserved (see collector.py's
  -- call site for the threshold -- deliberately NOT store.GAP_SECONDS,
  -- which is tuned for the SoC chart's dashed-hole question, not this one).
  solar_soc       REAL    NOT NULL DEFAULT 0,
  ledger_soc      INTEGER,
  ledger_stale    INTEGER NOT NULL DEFAULT 0,
  -- Task 20: lifetime free miles driven (see green.free_miles_step). A
  -- running total, never reset -- ledger_odo is the odometer as of the last
  -- observation (NULL until this ledger has watched its own first tick,
  -- same "absence means never assume a past" convention as ledger_soc
  -- above). free_miles_since is the timestamp collector.py stamped the
  -- first time ledger_odo was recorded, purely for display ("since 27
  -- Jul") -- never recomputed, never backfilled.
  free_miles_driven REAL    NOT NULL DEFAULT 0,
  tracked_miles     REAL    NOT NULL DEFAULT 0,
  ledger_odo        REAL,
  free_miles_since  INTEGER,
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
    "garage_url": None, "garage_auto_open": 0, "garage_ring_m": 800,
    "garage_close_hour": None, "garage_close_warn_s": 8,
}

STATE_DEFAULTS = {
    "state": "idle", "breach_ticks": 0, "recover_ticks": 0,
    "grace_s_elapsed": 0, "hold_s": 0,
    "dirty": 0, "original_amps": None, "original_limit": None,
    "raised_to": None, "raise_hold_elapsed": 0,
    "requests_today": 0, "requests_day": None,
    "capped": 0, "engaged_at": None,
    "consecutive_429s": 0, "backoff_s": 0,
    "garage_armed": 0, "garage_last_close_day": None,
    "solar_soc": 0.0, "ledger_soc": None, "ledger_stale": 0,
    "free_miles_driven": 0.0, "tracked_miles": 0.0, "ledger_odo": None,
    "free_miles_since": None,
}

# The Machine fields that must survive between ticks. Anything here that is
# not persisted silently disables the dwell and hysteresis logic.
MACHINE_FIELDS = ("state", "breach_ticks", "recover_ticks",
                  "grace_s_elapsed", "hold_s")


def machine_from(st: dict) -> Machine:
    return Machine(**{k: st[k] for k in MACHINE_FIELDS})


def machine_fields(m: Machine) -> dict:
    return {k: getattr(m, k) for k in MACHINE_FIELDS}


# Columns added to solar_state AFTER it first shipped. `CREATE TABLE IF NOT
# EXISTS` in SCHEMA is a no-op against a table that already exists -- see
# store.py's _migrate, which this mirrors -- so a schema edit alone never
# reaches a live car.db whose solar_state predates the column. migrate_state()
# must run once at startup, after SCHEMA is applied, before any load_state or
# save_state call.
STATE_NEW_COLUMNS = (
    ("raise_hold_elapsed", "INTEGER NOT NULL DEFAULT 0"),
    ("consecutive_429s", "INTEGER NOT NULL DEFAULT 0"),
    ("backoff_s", "INTEGER NOT NULL DEFAULT 0"),
    ("garage_armed", "INTEGER NOT NULL DEFAULT 0"),
    ("garage_last_close_day", "TEXT"),
    # Task 18: the banked-solar ledger -- see the SCHEMA comment above.
    ("solar_soc", "REAL NOT NULL DEFAULT 0"),
    ("ledger_soc", "INTEGER"),
    ("ledger_stale", "INTEGER NOT NULL DEFAULT 0"),
    # Task 20: lifetime free miles driven -- see the SCHEMA comment above.
    ("free_miles_driven", "REAL NOT NULL DEFAULT 0"),
    ("tracked_miles", "REAL NOT NULL DEFAULT 0"),
    ("ledger_odo", "REAL"),
    ("free_miles_since", "INTEGER"),
)


def migrate_state(db: sqlite3.Connection) -> None:
    """Add columns to an EXISTING solar_state table."""
    existing = {row[1] for row in db.execute("PRAGMA table_info(solar_state)")}
    for name, decl in STATE_NEW_COLUMNS:
        if name not in existing:
            db.execute(f"ALTER TABLE solar_state ADD COLUMN {name} {decl}")


# Columns added to solar_config AFTER it first shipped -- the first ones ever
# needed here. Same hazard as STATE_NEW_COLUMNS above: CREATE TABLE IF NOT
# EXISTS in SCHEMA is a no-op against the owner's live solar_config, which
# already has rows in it, so a schema edit alone never reaches it.
# migrate_config() must run once at startup, after SCHEMA is applied, before
# any load_config or save_config call -- mirrored in store.py right next to
# migrate_state().
CONFIG_NEW_COLUMNS = (
    ("garage_url", "TEXT"),
    ("garage_auto_open", "INTEGER NOT NULL DEFAULT 0"),
    ("garage_ring_m", "INTEGER NOT NULL DEFAULT 800"),
    ("garage_close_hour", "INTEGER"),
    ("garage_close_warn_s", "INTEGER NOT NULL DEFAULT 8"),
)


def migrate_config(db: sqlite3.Connection) -> None:
    """Add columns to an EXISTING solar_config table."""
    existing = {row[1] for row in db.execute("PRAGMA table_info(solar_config)")}
    for name, decl in CONFIG_NEW_COLUMNS:
        if name not in existing:
            db.execute(f"ALTER TABLE solar_config ADD COLUMN {name} {decl}")


def _begin_immediate(db: sqlite3.Connection) -> bool:
    """Take SQLite's write lock BEFORE the read half of a read-modify-write.

    Without it, two processes can both SELECT the old row, both compute an
    update from it, and the second write silently discards the first. Returns
    whether this call opened the transaction, so nested use does not commit
    someone else's work out from under them.
    """
    if db.in_transaction:
        return False
    db.execute("BEGIN IMMEDIATE")
    return True


def load_config(db: sqlite3.Connection) -> dict:
    row = db.execute("SELECT * FROM solar_config WHERE id = 1").fetchone()
    if row is None:
        return dict(CONFIG_DEFAULTS)
    return {k: row[k] for k in CONFIG_DEFAULTS}


# WRITER SEPARATION, relied upon by the whole design: solar_config is written
# only by the web app (the owner editing settings) and solar_state only by the
# collector (one row per vehicle, one process). They never write the same table,
# so cross-process contention on a single row does not arise in the current
# wiring. BEGIN IMMEDIATE above is defence in depth, and this comment is the
# thing to re-read before adding a config write to the collector or a state
# write to the web app.
def save_config(db: sqlite3.Connection, **fields) -> None:
    unknown = set(fields) - set(CONFIG_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown solar_config fields: {sorted(unknown)}")
    owned = _begin_immediate(db)
    try:
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
    except BaseException:
        # Roll back ONLY what we opened. Leaving it open would make
        # _begin_immediate see in_transaction on every later call, return
        # owned=False, and silently skip the commit forever -- turning one
        # transient error into a permanent persistence outage on a connection
        # that lives as long as the process.
        if owned:
            db.rollback()
        raise
    else:
        if owned:
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
        # Truthy, not `is None`, deliberately: a zero from either field would
        # reach control()'s `error_w / volts` and raise ZeroDivisionError.
        # Coercing a nonsensical zero to the documented nominal is the safe
        # failure.
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


# WRITER SEPARATION, relied upon by the whole design: solar_config is written
# only by the web app (the owner editing settings) and solar_state only by the
# collector (one row per vehicle, one process). They never write the same table,
# so cross-process contention on a single row does not arise in the current
# wiring. BEGIN IMMEDIATE above is defence in depth, and this comment is the
# thing to re-read before adding a config write to the collector or a state
# write to the web app.
def save_state(db: sqlite3.Connection, vin: str, **fields) -> None:
    unknown = set(fields) - set(STATE_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown solar_state fields: {sorted(unknown)}")
    owned = _begin_immediate(db)
    try:
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
    except BaseException:
        # Roll back ONLY what we opened. Leaving it open would make
        # _begin_immediate see in_transaction on every later call, return
        # owned=False, and silently skip the commit forever -- turning one
        # transient error into a permanent persistence outage on a connection
        # that lives as long as the process.
        if owned:
            db.rollback()
        raise
    else:
        if owned:
            db.commit()


def log_tick(db: sqlite3.Connection, vin: str, **fields) -> None:
    """Every tick is logged, written or not, so a quiet loop and a broken loop
    look different in the record."""
    columns = ("ts", "state", "grid_w", "solar_w", "car_w", "surplus_w", "error_w",
               "amps_before", "amps_target", "amps_written", "soc", "import_w",
               "period_s", "note")
    unknown = set(fields) - set(columns)
    if unknown:
        raise ValueError(f"unknown solar_ticks fields: {sorted(unknown)}")
    db.execute(
        f"""INSERT OR REPLACE INTO solar_ticks (vin, {', '.join(columns)})
            VALUES (?, {', '.join('?' * len(columns))})""",
        [vin] + [fields.get(c) for c in columns],
    )
    db.commit()


def count_request(db: sqlite3.Connection, vin: str, today: str) -> tuple[int, bool]:
    """Increment the daily request counter. Returns (count, capped).

    Denominated in REQUESTS, not dollars: Tesla no longer publishes
    per-request data pricing, so a dollar cap would be a guess dressed as a
    limit.

    NOT atomic end to end -- the read happens outside the transaction that
    save_state opens, so two concurrent callers could both read the same count
    and one increment would be lost. Safe only under the writer-separation
    invariant documented above save_state: solar_state is written by the
    collector alone, and there is exactly one collector process. Re-read that
    invariant before calling this from anywhere else.
    """
    st = load_state(db, vin)
    count = st["requests_today"] + 1 if st["requests_day"] == today else 1
    cap = load_config(db)["daily_request_cap"]
    capped = count >= cap
    save_state(db, vin, requests_today=count, requests_day=today,
               capped=1 if capped else 0)
    return count, capped


def may_restore(dirty: int, location: str, online: bool, proxy_up: bool) -> bool:
    """Whether a crash-recovery restore may be attempted right now.

    Restoring writes amps and a charge limit to the car. Doing that
    unconditionally at startup would push home settings into whatever session
    the car is actually in -- including a Supercharger. If any gate fails the
    dirty flag STAYS SET and we retry next tick; it is never cleared by
    giving up.
    """
    return bool(dirty) and location == "home" and online and proxy_up


def recent_grid_w(db: sqlite3.Connection, vin: str, limit: int) -> list[float]:
    """The last `limit` LOGGED grid_w readings for vin, newest first.

    Feeds grid_is_stuck(): the stuck-meter detector needs the raw tick
    history, not a derived quantity, and solar_ticks already logs grid_w
    every tick regardless of whether anything changed -- the history is
    already there for the reading.
    """
    rows = db.execute(
        """SELECT grid_w FROM solar_ticks
           WHERE vin = ? AND grid_w IS NOT NULL
           ORDER BY ts DESC LIMIT ?""",
        (vin, limit),
    ).fetchall()
    return [row["grid_w"] for row in rows]


def last_tick_ts(db: sqlite3.Connection, vin: str) -> int | None:
    """The timestamp of the most recently logged tick for vin, or None
    before the first one has ever been logged.

    Feeds the banked-solar ledger's gap detection (green.ledger_step):
    elapsed real time since the last observation, read back from disk
    rather than held in memory, so a process restart is measured correctly
    too -- a crash is exactly the kind of unobserved gap the ledger's
    staleness flag exists to catch. Must be read BEFORE this tick's own
    log_tick() call, or MAX(ts) would return this tick's own row and the
    gap would always read as zero.
    """
    row = db.execute(
        "SELECT MAX(ts) AS ts FROM solar_ticks WHERE vin = ?", (vin,)
    ).fetchone()
    return row["ts"] if row and row["ts"] is not None else None


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
