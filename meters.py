"""Monotonic energy counters for Home Assistant's Energy Dashboard.

WHY A RATCHET AND NOT A RECOMPUTATION. HA's Energy Dashboard only accepts
sensors with state_class total or total_increasing -- instantaneous power is
not eligible. And total_increasing has a specific, unforgiving reset rule: a
drop below ~90% of the previous value is read as a NEW METER CYCLE, the
zero-point is set to 0, and the next sample is then added IN FULL. A counter
that briefly reports a smaller number therefore injects its entire lifetime
value into one five-minute bucket, and the cost sensor books money to match.

Both of our sources can move backwards:

  * Tesla revises the open bucket of calendar_history downward as data
    settles -- often by less than 10%, which is worse: HA treats that as a
    genuine decrease and silently subtracts.
  * Any recomputation from history depends on the VIN and the tick log; a
    restored database or an empty samples table would recompute a smaller
    number in perfect good faith.

So the stored value only ever moves forward: stored = max(stored, computed).
Under-reporting during catch-up is the safe failure. A spike is the
destructive one, because it is indistinguishable from real consumption and
lands permanently in long-term statistics.

Two quantities per channel. closed_wh folds in one fully-closed day at a
time and is the stable baseline. cumulative_wh -- what HA actually sees -- is
closed_wh plus today's partial day, ratcheted. Reading the OPEN bucket is
what gives the Energy Dashboard hourly shape instead of one step per day, and
it is safe only BECAUSE of the ratchet: a downward revision leaves the
counter untouched, an upward one is real energy and is taken.
"""
from __future__ import annotations

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
  channel              TEXT PRIMARY KEY,
  -- What HA sees: ratcheted, and INCLUDES today's partial day so the Energy
  -- Dashboard gets hourly shape rather than one daily step.
  cumulative_wh        REAL NOT NULL DEFAULT 0,
  -- Only fully-closed days. The stable baseline that today's partial is
  -- added to; it advances once per day and never wobbles.
  closed_wh            REAL NOT NULL DEFAULT 0,
  last_closed_bucket   INTEGER,
  updated_ts           INTEGER
);
"""

# Everything the Energy Dashboard is offered. Named here so a typo becomes a
# KeyError at the call site rather than a silently orphaned row.
CHANNELS = ("site_import", "site_export", "site_solar")


# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS is
# a NO-OP on an existing table, so a new column in SCHEMA above reaches a
# fresh install and nothing else -- the deployed database keeps the old shape
# and every query naming the column fails with "no such column". This project
# has been bitten by exactly that twice; the ALTER is the only thing that
# actually migrates.
NEW_COLUMNS = (
    ("closed_wh", "REAL NOT NULL DEFAULT 0"),
)


def migrate(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    existing = {row[1] for row in db.execute("PRAGMA table_info(meters)")}
    for name, decl in NEW_COLUMNS:
        if name not in existing:
            db.execute(f"ALTER TABLE meters ADD COLUMN {name} {decl}")
    db.commit()


def read_all(db: sqlite3.Connection) -> dict[str, dict]:
    rows = db.execute(
        "SELECT channel, cumulative_wh, last_closed_bucket, updated_ts FROM meters"
    ).fetchall()
    return {r["channel"]: dict(r) for r in rows}


def kwh(db: sqlite3.Connection, channel: str) -> float | None:
    """A channel in kWh, or None when it has never been written.

    None, never 0. HA discards a non-numeric state from statistics, so an
    unavailable sensor is harmless -- while a 0 would read as a counter reset
    and inject the whole lifetime total into a single bucket.
    """
    row = db.execute(
        "SELECT cumulative_wh FROM meters WHERE channel = ?", (channel,)).fetchone()
    return round(row["cumulative_wh"] / 1000.0, 3) if row else None


def _row(db: sqlite3.Connection, channel: str):
    return db.execute(
        "SELECT cumulative_wh, closed_wh, last_closed_bucket FROM meters"
        " WHERE channel = ?", (channel,)).fetchone()


def _write(db, channel, cumulative, closed, bucket):
    db.execute(
        """INSERT INTO meters (channel, cumulative_wh, closed_wh,
                               last_closed_bucket, updated_ts)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(channel) DO UPDATE SET
             cumulative_wh = excluded.cumulative_wh,
             closed_wh = excluded.closed_wh,
             last_closed_bucket = excluded.last_closed_bucket,
             updated_ts = excluded.updated_ts""",
        (channel, cumulative, closed, bucket, int(time.time())))
    db.commit()


def close_day(db: sqlite3.Connection, channel: str, day_wh: float,
              bucket_ts: int) -> float:
    """Fold one FULLY CLOSED day into the stable baseline. Returns closed_wh.

    A delta, applied once per day. Negative is ignored rather than subtracted:
    a day cannot un-happen, and the only way to see one is a source that
    changed its mind.
    """
    if channel not in CHANNELS:
        raise KeyError(f"unknown meter channel {channel!r}")
    row = _row(db, channel)
    closed = (row["closed_wh"] if row else 0.0) + max(0.0, float(day_wh))
    cumulative = max(row["cumulative_wh"] if row else 0.0, closed)
    _write(db, channel, cumulative, closed, bucket_ts)
    return closed


def observe_today(db: sqlite3.Connection, channel: str, today_wh: float) -> float:
    """Ratchet the exposed counter to the closed baseline plus today so far.

    ABSOLUTE, not a delta, and this is what gives the Energy Dashboard hourly
    resolution instead of one step per day. Ingesting only closed days would
    make the counter jump a whole day's energy at once, and HA would book all
    of it into the five-minute bucket it happened to land in.

    Reading the OPEN bucket is safe precisely because of the ratchet: Tesla
    revises it downward as data settles, and a downward revision simply leaves
    the counter where it was. Upward revisions are real energy and are taken.
    """
    if channel not in CHANNELS:
        raise KeyError(f"unknown meter channel {channel!r}")
    row = _row(db, channel)
    if row is None:
        return 0.0
    target = row["closed_wh"] + max(0.0, float(today_wh))
    cumulative = max(row["cumulative_wh"], target)
    _write(db, channel, cumulative, row["closed_wh"], row["last_closed_bucket"])
    return cumulative


def seed(db: sqlite3.Connection, channel: str, bucket_ts: int) -> None:
    """Create a channel at zero, so the first real observation has a baseline."""
    if channel not in CHANNELS:
        raise KeyError(f"unknown meter channel {channel!r}")
    if _row(db, channel) is None:
        _write(db, channel, 0.0, 0.0, bucket_ts)


def last_closed_bucket(db: sqlite3.Connection, channel: str) -> int | None:
    row = db.execute(
        "SELECT last_closed_bucket FROM meters WHERE channel = ?",
        (channel,)).fetchone()
    return row["last_closed_bucket"] if row else None
