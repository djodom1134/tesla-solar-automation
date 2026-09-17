from __future__ import annotations

import landmarks
from home import HomeConfig

# A synthetic anchor near the Longmont landmarks in landmarks.PLACES, rounded
# to whole hundredths on purpose: these tests need the GEOMETRY (town before
# mountains, park before Estes), not anybody's address.
HOME = HomeConfig(latitude=40.1700, longitude=-105.1300, radius_m=75)


def test_nothing_is_reachable_on_an_empty_bank():
    places = landmarks.reachable(HOME, 0.0)
    assert places, "the list is always returned; only the flags change"
    assert not any(p["reachable"] for p in places)


def test_places_are_ordered_by_what_it_takes_to_get_there():
    places = landmarks.reachable(HOME, 100.0)
    needed = [p["needed"] for p in places]
    assert needed == sorted(needed)
    # Sanity against the real geography: town before mountains.
    names = [p["name"] for p in places]
    assert names.index("Roosevelt Park") < names.index("Estes Park")
    assert names.index("Lyons") < names.index("Denver (Union Station)")


def test_round_trip_is_the_default_because_you_have_to_get_home():
    one_way = {p["name"]: p["needed"] for p in
               landmarks.reachable(HOME, 100.0, round_trip=False)}
    both = {p["name"]: p["needed"] for p in landmarks.reachable(HOME, 100.0)}
    for name in one_way:
        assert both[name] == round(one_way[name] * 2, 1) or \
            abs(both[name] - one_way[name] * 2) < 0.11, name


def test_mountain_destinations_carry_a_harsher_road_factor():
    places = {p["name"]: p for p in landmarks.reachable(HOME, 0.0)}
    # Ward and Carter Lake are a similar straight-line distance from Longmont,
    # but one is up a canyon. The estimate must not pretend otherwise.
    assert places["Ward"]["mountain"] is True
    assert places["Carter Lake"]["mountain"] is False
    from home import distance_m
    ward_straight = distance_m(HOME.latitude, HOME.longitude,
                               40.0722, -105.5108) / landmarks.METERS_PER_MILE
    assert places["Ward"]["miles"] > ward_straight * 1.5


def test_a_real_bank_lights_up_the_near_places_only():
    # 20 banked miles is a 10-mile round trip radius at the road factor.
    places = landmarks.reachable(HOME, 20.0)
    lit = [p["name"] for p in places if p["reachable"]]
    assert "Roosevelt Park" in lit, "a park four miles away must be reachable"
    assert "Estes Park" not in lit, "a 40-mile mountain drive must not be"
    assert all(p["needed"] <= 20.0 for p in places if p["reachable"])


def test_no_home_means_no_claims():
    assert landmarks.reachable(None, 500.0) == []


def test_unknown_bank_is_not_treated_as_infinite():
    places = landmarks.reachable(HOME, None)
    assert not any(p["reachable"] for p in places)
