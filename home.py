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
