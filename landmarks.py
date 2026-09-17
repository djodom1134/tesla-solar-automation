"""Places you could actually drive to on banked sunshine.

WHY A CURATED LIST AND NOT AN API. A road-following isochrone needs a routing
service, and the one researched for this project (HERE) turned out to require a
credit card. Nothing here justifies that: the question "could I reach Estes Park
on the sun I banked today" is answered well enough by a fixed list of places
within an hour of home, each with a straight-line distance and an honest road
factor.

WHAT THIS IS NOT. `road_miles` is a straight-line distance inflated by a detour
factor -- it is an ESTIMATE, not a route. Mountain destinations are worse than
the factor suggests (switchbacks, and elevation costs energy that flat miles do
not). Treated as "worth considering", never as a range guarantee.

Distances are computed from the owner's configured home, so the list reorders
itself if home moves.
"""
from __future__ import annotations

from home import distance_m

METERS_PER_MILE = 1609.344

# Typical ratio of driving distance to straight-line distance. 1.25-1.4 is the
# usual range quoted for road networks; the Front Range grid is kinder than
# most on the plains and much worse into the mountains, which is why canyon
# destinations carry their own factor below.
ROAD_FACTOR = 1.3
MOUNTAIN_FACTOR = 1.6

# (name, latitude, longitude, mountainous)
PLACES: tuple[tuple[str, float, float, bool], ...] = (
    # --- in town ------------------------------------------------------------
    ("Roosevelt Park", 40.1682, -105.1019, False),
    ("Longmont Museum", 40.1638, -105.0900, False),
    ("Union Reservoir", 40.1836, -105.0533, False),
    ("Sandstone Ranch", 40.1567, -105.0264, False),
    ("McIntosh Lake", 40.1889, -105.1400, False),
    # --- nearby towns -------------------------------------------------------
    ("Hygiene", 40.1783, -105.1861, False),
    ("Niwot", 40.1039, -105.1706, False),
    ("Lyons", 40.2247, -105.2714, False),
    ("Berthoud", 40.3086, -105.0811, False),
    ("Erie", 40.0503, -105.0500, False),
    ("Frederick", 40.0983, -104.9369, False),
    ("Lafayette", 39.9936, -105.0897, False),
    ("Loveland", 40.3978, -105.0750, False),
    ("Boulder (Pearl St)", 40.0190, -105.2747, False),
    ("Brighton", 39.9853, -104.8206, False),
    ("Greeley", 40.4233, -104.7091, False),
    ("Fort Collins (Old Town)", 40.5878, -105.0761, False),
    # --- worth the drive ----------------------------------------------------
    ("Carter Lake", 40.3339, -105.2136, False),
    ("Chautauqua Park", 39.9994, -105.2814, False),
    ("Button Rock Preserve", 40.2178, -105.3453, True),
    ("Eldorado Canyon", 39.9297, -105.2919, True),
    ("Nederland", 39.9614, -105.5108, True),
    ("Ward", 40.0722, -105.5108, True),
    ("Golden", 39.7555, -105.2211, False),
    ("Denver (Union Station)", 39.7527, -105.0000, False),
    ("Estes Park", 40.3772, -105.5217, True),
    ("Rocky Mountain NP (Beaver Meadows)", 40.3603, -105.5808, True),
    ("Idaho Springs", 39.7425, -105.5138, True),
    ("Georgetown", 39.7061, -105.6975, True),
)


def reachable(home_cfg, banked_miles: float | None,
              round_trip: bool = True) -> list[dict]:
    """Every place, nearest first, each marked reachable or not.

    The whole list is returned rather than only what fits: the caller animates
    banked_miles upward continuously, and returning everything lets places
    light up as the number climbs without another request.

    `round_trip` doubles the requirement, which is the honest default -- a
    destination you cannot get back from is not somewhere you can drive to on
    banked sun.
    """
    if home_cfg is None:
        return []
    out = []
    for name, lat, lon, mountain in PLACES:
        straight = distance_m(home_cfg.latitude, home_cfg.longitude,
                              lat, lon) / METERS_PER_MILE
        factor = MOUNTAIN_FACTOR if mountain else ROAD_FACTOR
        road = straight * factor
        needed = road * 2 if round_trip else road
        out.append({
            "name": name,
            "miles": round(road, 1),
            "needed": round(needed, 1),
            "mountain": mountain,
            "reachable": banked_miles is not None and banked_miles >= needed,
        })
    out.sort(key=lambda p: p["needed"])
    return out
