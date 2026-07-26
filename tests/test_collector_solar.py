from __future__ import annotations

from types import SimpleNamespace

import collector

S = SimpleNamespace(poll_driving=120, poll_charging=300, poll_idle=900,
                    poll_asleep=300)


def test_engaged_uses_the_solar_period_over_every_other_rule():
    assert collector.next_interval(
        "online", {"shift": "P", "charging": True}, S, solar_engaged=120) == 120


def test_not_engaged_keeps_the_existing_behaviour():
    assert collector.next_interval(
        "online", {"shift": "P", "charging": True}, S, solar_engaged=0) == 300
    assert collector.next_interval("offline", None, S, solar_engaged=0) == 300


def test_engagement_never_overrides_a_sleeping_car():
    """A sleeping car must not be polled fast; the loop idles instead."""
    assert collector.next_interval("asleep", None, S, solar_engaged=120) == 300


def test_engaged_ticks_skip_the_state_check_and_most_vehicle_data(monkeypatch):
    """Three billable calls per tick is $22/month; this pattern is ~1.4."""
    calls = []

    class FakeClient:
        async def vehicle(self, vin):
            calls.append("state"); return {"state": "online"}
        async def vehicle_data(self, vin, *a, **k):
            calls.append("data"); return {"response": {}}

    # With view_refresh_ticks=5 and no writes, 5 engaged ticks must issue
    # exactly ONE vehicle_data and ZERO state checks.
    assert collector.should_refresh_view(ticks_since_view=0, refresh_every=5,
                                         wrote_last_tick=False) is False
    assert collector.should_refresh_view(ticks_since_view=5, refresh_every=5,
                                         wrote_last_tick=False) is True
    assert collector.should_refresh_view(ticks_since_view=0, refresh_every=5,
                                         wrote_last_tick=True) is True
