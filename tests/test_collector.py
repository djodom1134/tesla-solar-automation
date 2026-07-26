from types import SimpleNamespace

import collector

S = SimpleNamespace(poll_driving=120, poll_charging=300, poll_idle=900, poll_asleep=300)


def test_asleep_uses_the_free_check_interval():
    assert collector.next_interval("asleep", None, S) == 300
    assert collector.next_interval("offline", None, S) == 300


def test_driving_polls_fastest():
    assert collector.next_interval("online", {"shift": "D", "charging": False}, S) == 120
    assert collector.next_interval("online", {"shift": "R", "charging": False}, S) == 120


def test_charging_polls_at_the_charging_interval():
    assert collector.next_interval("online", {"shift": "P", "charging": True}, S) == 300


def test_driving_wins_over_charging():
    assert collector.next_interval("online", {"shift": "D", "charging": True}, S) == 120


def test_online_and_idle_polls_slowest():
    assert collector.next_interval("online", {"shift": "P", "charging": False}, S) == 900


def test_online_but_no_data_falls_back_to_idle():
    assert collector.next_interval("online", None, S) == 900
