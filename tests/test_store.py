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
