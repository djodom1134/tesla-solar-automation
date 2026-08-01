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

Site channels advance ONLY on closed day buckets -- days strictly before
today in the configured timezone -- so the open bucket's revisions can never
reach the counter at all.
"""
from __future__ import annotations

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
  channel              TEXT PRIMARY KEY,
  cumulative_wh        REAL NOT NULL DEFAULT 0,
  last_closed_bucket   INTEGER,
  updated_ts           INTEGER
);
"""

# Everything the Energy Dashboard is offered. Named here so a typo becomes a
# KeyError at the call site rather than a silently orphaned row.
CHANNELS = ("site_import", "site_export", "site_solar")


def migrate(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


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


def advance(db: sqlite3.Connection, channel: str, add_wh: float,
            closed_bucket: int | None = None) -> float:
    """Add energy to a channel, forward only. Returns the new total in Wh.

    add_wh is a DELTA, not an absolute: callers hand over one closed day at a
    time. Negative deltas are ignored rather than subtracted -- a day cannot
    un-happen, and the only way to see one is a source that changed its mind.
    """
    if channel not in CHANNELS:
        raise KeyError(f"unknown meter channel {channel!r}")
    row = db.execute(
        "SELECT cumulative_wh, last_closed_bucket FROM meters WHERE channel = ?",
        (channel,)).fetchone()
    current = row["cumulative_wh"] if row else 0.0
    # max(), not +=, is the ratchet: a negative or nonsensical delta leaves
    # the counter exactly where it was.
    new = max(current, current + max(0.0, float(add_wh)))
    bucket = closed_bucket if closed_bucket is not None else (
        row["last_closed_bucket"] if row else None)
    db.execute(
        """INSERT INTO meters (channel, cumulative_wh, last_closed_bucket, updated_ts)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(channel) DO UPDATE SET
             cumulative_wh = excluded.cumulative_wh,
             last_closed_bucket = excluded.last_closed_bucket,
             updated_ts = excluded.updated_ts""",
        (channel, new, bucket, int(time.time())))
    db.commit()
    return new


def last_closed_bucket(db: sqlite3.Connection, channel: str) -> int | None:
    row = db.execute(
        "SELECT last_closed_bucket FROM meters WHERE channel = ?",
        (channel,)).fetchone()
    return row["last_closed_bucket"] if row else None
