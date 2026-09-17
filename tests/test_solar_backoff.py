"""Invariant 4 (spec 3.7): honour 429/Retry-After by lengthening the period,
never retrying at the normal cadence. backoff_seconds() is the pure decision;
collector.py (tested in test_collector_solar.py) only persists its inputs and
acts on its output.
"""
from __future__ import annotations

import pytest

import solar

PERIOD = 120


@pytest.mark.parametrize("consecutive_429s,retry_after,expected", [
    # No prior failures: the baseline case must not invent a lengthening --
    # 2**0 == 1, so this is exactly period_s.
    (0, None, 120),
    # Several consecutive failures: doubles from period_s each time.
    (1, None, 240),
    (2, None, 480),
    (3, None, 960),
    # ... and settles at the cap rather than continuing to compound --
    # 120 * 2**4 == 1920, which must clip to 1800.
    (4, None, 1800),
    (7, None, 1800),
])
def test_exponential_backoff_from_the_configured_period(consecutive_429s, retry_after, expected):
    assert solar.backoff_seconds(consecutive_429s, PERIOD, retry_after) == expected


def test_a_supplied_retry_after_overrides_the_computed_value():
    """The server knows better than any heuristic: a Retry-After of 30s must
    win even though the exponential formula alone would ask for 240s."""
    assert solar.backoff_seconds(1, PERIOD, retry_after=30) == 30


def test_retry_after_longer_than_the_cap_still_clips():
    """Honouring the server does not mean trusting it unboundedly -- a
    pathological or malicious Retry-After must not park the collector silent
    for hours."""
    assert solar.backoff_seconds(1, PERIOD, retry_after=5000) == 1800


def test_retry_after_shorter_than_the_period_is_still_honoured():
    """The server explicitly said 5s is fine -- trusting it lets recovery
    happen sooner than our own configured cadence, which is not a hazard:
    Tesla is the one telling us when its own limit lifts."""
    assert solar.backoff_seconds(3, PERIOD, retry_after=5) == 5


def test_zero_consecutive_with_a_retry_after_still_honours_it():
    assert solar.backoff_seconds(0, PERIOD, retry_after=42) == 42
