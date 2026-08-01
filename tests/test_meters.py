from __future__ import annotations

import sqlite3

import pytest

import meters


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    meters.migrate(db)
    return db


def test_a_channel_never_written_reads_none_not_zero():
    """0 would read to HA as a counter reset on a total_increasing sensor, and
    its reset rule sets the zero-point to 0 -- so the NEXT real sample is added
    in full, injecting the entire lifetime total into one five-minute bucket
    with the cost sensor booking money to match. None becomes unavailable,
    which HA discards from statistics safely."""
    db = _db()
    assert meters.kwh(db, "site_import") is None


def test_counters_only_ever_move_forward():
    """The ratchet. Tesla revises open calendar_history buckets downward as
    data settles, and a recomputation from a restored database can legitimately
    produce a smaller number -- neither may be allowed to move the counter
    back, because HA cannot tell a correction from a meter swap."""
    db = _db()
    meters.advance(db, "site_import", 5000.0)
    assert meters.kwh(db, "site_import") == 5.0

    meters.advance(db, "site_import", -3000.0)   # a downward revision
    assert meters.kwh(db, "site_import") == 5.0, "a negative delta must not subtract"

    meters.advance(db, "site_import", 0.0)
    assert meters.kwh(db, "site_import") == 5.0

    meters.advance(db, "site_import", 1500.0)
    assert meters.kwh(db, "site_import") == 6.5, "real energy still accumulates"


def test_closed_bucket_is_remembered_so_a_day_is_never_counted_twice():
    """Double-counting is as destructive as a spike: it inflates the counter
    permanently and there is no way to walk it back without a reset, which is
    itself the thing being avoided."""
    db = _db()
    meters.advance(db, "site_solar", 42000.0, closed_bucket=1_700_000_000)
    assert meters.last_closed_bucket(db, "site_solar") == 1_700_000_000
    # A caller that has already ingested this bucket can see so and skip it.
    assert meters.kwh(db, "site_solar") == 42.0


def test_an_unknown_channel_is_a_loud_error_not_an_orphan_row():
    db = _db()
    with pytest.raises(KeyError):
        meters.advance(db, "site_batery_typo", 100.0)


def test_channels_are_independent():
    db = _db()
    meters.advance(db, "site_import", 1000.0)
    meters.advance(db, "site_export", 2000.0)
    assert meters.kwh(db, "site_import") == 1.0
    assert meters.kwh(db, "site_export") == 2.0
    assert meters.kwh(db, "site_solar") is None
