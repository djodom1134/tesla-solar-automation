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


# --- Round-1 review fixes: rank-based bucketing must hold two invariants
# for ANY spacing, not just the uniformly-spaced fixtures above: never
# return more than `buckets` rows, and never drop a sample when the
# matching count is <= `buckets`. Time-width bucketing violated both.


@pytest.mark.parametrize("buckets", [2, 3, 4, 5, 6])
def test_history_never_exceeds_bucket_budget_on_exact_divisors(db, buckets):
    """Regression: when the data span divided evenly by `buckets`, the old
    time-width bucketing produced one bucket index past the intended
    0..buckets-1 range, returning buckets+1 rows. Reproduced against the
    1000-sample/60s-apart fixture varying only `buckets` (59940 % buckets
    happens to be 0 for each value here)."""
    for i in range(1000):
        db.record(view(1000 + i * 60, 50))
    rows = db.history("VIN1", 0, 10 ** 9, buckets=buckets)
    assert len(rows) == buckets


def test_history_keeps_all_samples_when_count_is_under_budget_and_unevenly_spaced(db):
    """Regression: two tightly-clustered samples plus one far-off outlier,
    well under the bucket budget, used to collapse into one bucket under
    time-width bucketing (both clustered samples share one wide bucket) even
    though there are far fewer samples than buckets. Every sample must
    survive whenever count <= buckets."""
    db.record(view(1000, 50))
    db.record(view(1100, 51))
    db.record(view(101000, 52))
    rows = db.history("VIN1", 0, 10 ** 9, buckets=10)
    assert [r["ts"] for r in rows] == [1000, 1100, 101000]


def test_gap_boundary_is_exclusive(db):
    """A delta of exactly GAP_SECONDS is still adjacent, not a gap -- only
    strictly further apart counts as a sleep hole. Pins the `>` in the gap
    comparison against an accidental switch to `>=`."""
    db.record(view(1000, 80))
    db.record(view(1000 + store.GAP_SECONDS, 78))
    rows = db.history("VIN1", 0, 10 ** 9)
    assert [r["gap"] for r in rows] == [False, False]
