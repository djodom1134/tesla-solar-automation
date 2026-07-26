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
        """Samples in [start, end], downsampled to at most `buckets` points.

        Bucketing takes the last sample in each bucket rather than an average,
        so a charge that finishes mid-bucket still reads as finished.

        Bucket width is derived from the span of the *matching* samples, not
        the raw (start, end) window. Callers pass wide sentinel windows (e.g.
        start=0, end=10**9) to mean "everything, whatever it spans" — if
        width were end-start over buckets, a couple of samples a minute
        apart inside a 30-year-wide window would land in the same
        multi-decade bucket and one would be silently dropped, even though
        there are far fewer samples than the bucket budget.
        """
        bounds = self._db.execute(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM samples WHERE vin = ? AND ts >= ? AND ts <= ?",
            (vin, start, end),
        ).fetchone()
        if bounds is None or bounds["lo"] is None:
            return []
        span = max(1, bounds["hi"] - bounds["lo"])
        buckets = max(1, buckets)
        # Ceiling division: a floored width under-covers the span by the
        # remainder, which — combined with the grouping offset below sitting
        # exactly on the first sample — synthesizes one extra partial bucket
        # at the far edge and blows the caller's budget by one row.
        width = max(1, -(-span // buckets))
        lo = bounds["lo"]
        rows = self._db.execute(
            """SELECT ts, battery_level, usable_battery_level, charging
               FROM samples
               WHERE vin = ? AND ts >= ? AND ts <= ?
                 AND ts IN (
                   SELECT MAX(ts) FROM samples
                   WHERE vin = ? AND ts >= ? AND ts <= ?
                   GROUP BY (ts - ?) / ?
                 )
               ORDER BY ts""",
            (vin, start, end, vin, start, end, lo, width),
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
