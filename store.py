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
  charge_energy_added    REAL,
  charger_actual_current INTEGER,
  charger_voltage        INTEGER,
  fast_charger_present   INTEGER,
  fast_charger_type      TEXT,
  at_home                TEXT,
  PRIMARY KEY (vin, ts)
);
CREATE INDEX IF NOT EXISTS samples_vin_ts ON samples (vin, ts);

CREATE TABLE IF NOT EXISTS snapshot (
  vin  TEXT PRIMARY KEY,
  ts   INTEGER NOT NULL,
  json TEXT    NOT NULL
);
"""


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


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._db = sqlite3.connect(self.path, timeout=10.0)
        self._db.row_factory = sqlite3.Row
        # WAL lets the collector and the web app write without blocking.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(SCHEMA)
        _migrate(self._db)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def record(self, view: dict[str, Any], at_home: str | None = None) -> None:
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
        """Samples in [start, end], downsampled to at most `buckets` points.

        Guarantees, for any input (not just uniformly-spaced data):
          1. Never returns more than `buckets` rows.
          2. Never drops a sample when the matching sample count is <= `buckets`.

        Real SoC data is not uniformly spaced — dense while driving or
        charging, then nothing for hours while the car sleeps. An earlier
        version of this bucketed by a fixed *time* width (span / buckets),
        which broke both guarantees: a width that evenly divided the span
        produced one bucket index past the intended range (budget overflow),
        and a handful of tightly-clustered samples plus one far-off outlier
        could still land in the same wide time-bucket even when the total
        sample count was far under budget (silent drop).

        Buckets are instead sized by sample *rank*, not by elapsed time:
        matching rows are numbered 0..n-1 in timestamp order, and bucket
        index = rn * buckets // n. For any n and buckets, this produces
        exactly min(n, buckets) distinct bucket values — never more, and
        never fewer than n when n <= buckets — so both guarantees hold
        unconditionally, independent of how the samples are spaced in time.
        The last (highest-rn / latest-ts) sample in each bucket is kept, so
        a charge that finishes mid-bucket still reads as finished.

        This runs as a single SQL statement (one CTE, referenced twice)
        rather than a bounds pre-query followed by a bucketing query, so
        there's no gap between two reads for a concurrent writer (the
        collector or another web request) to land in. SQLite gives a single
        statement a consistent snapshot for its entire execution, even
        without an explicit transaction, so this is race-free by construction.
        """
        buckets = max(1, buckets)
        rows = self._db.execute(
            """WITH matched AS (
                 SELECT ts, battery_level, usable_battery_level, charging,
                        ROW_NUMBER() OVER (ORDER BY ts) - 1 AS rn,
                        COUNT(*) OVER () AS n
                 FROM samples
                 WHERE vin = ? AND ts >= ? AND ts <= ?
               )
               SELECT ts, battery_level, usable_battery_level, charging
               FROM matched
               WHERE rn IN (
                 SELECT MAX(rn) FROM matched GROUP BY (rn * ?) / n
               )
               ORDER BY ts""",
            (vin, start, end, buckets),
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
