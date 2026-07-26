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
