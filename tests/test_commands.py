import pytest

import commands


def test_catalog_ids_are_unique_and_grouped():
    ids = [c["id"] for c in commands.CATALOG]
    assert len(ids) == len(set(ids))
    for c in commands.CATALOG:
        assert c["group"] in commands.GROUPS


def test_commands_broken_through_the_proxy_are_excluded():
    """These return 400 invalid_command through Tesla's own proxy. Shipping
    them would put permanently-broken buttons on the page."""
    broken = {
        "sun_roof_control", "navigation_gps_request", "navigation_sc_request",
        "navigation_waypoints_request", "upcoming_calendar_entries",
        "update_calendar_entries", "remote_boombox",
        "remote_steering_wheel_heat_level_request",
        "remote_auto_steering_wheel_heat_climate_request",
    }
    assert not broken & {c["id"] for c in commands.CATALOG}


def test_risky_commands_require_confirmation():
    for cid in ("door_unlock", "actuate_trunk", "window_control", "set_valet_mode"):
        assert commands.find(cid)["confirm"] is True


def test_validate_coerces_and_bounds_charge_limit():
    spec = commands.find("set_charge_limit")
    assert commands.validate(spec, {"percent": "80"}) == {"percent": 80}
    with pytest.raises(ValueError):
        commands.validate(spec, {"percent": 20})
    with pytest.raises(ValueError):
        commands.validate(spec, {"percent": 101})


def test_validate_rejects_a_missing_required_param():
    with pytest.raises(ValueError):
        commands.validate(commands.find("set_charge_limit"), {})


def test_validate_emits_real_json_types_not_strings():
    """The proxy's getBool demands JSON true/false and getNumber a JSON number;
    {"on": "true"} is rejected."""
    body = commands.validate(commands.find("set_sentry_mode"), {"on": "true"})
    assert body["on"] is True
    assert isinstance(body["on"], bool)


def test_interpret_success():
    r = commands.interpret(200, {"response": {"result": True, "reason": ""}})
    assert r["ok"] is True


def test_interpret_treats_benign_reasons_as_success():
    for reason in ("already_set", "not_charging", "is_charging", "complete", "requested"):
        r = commands.interpret(200, {"response": {"result": False, "reason": reason}})
        assert r["ok"] is True, reason


def test_interpret_real_failure_is_not_ok_and_keeps_the_reason():
    r = commands.interpret(200, {"response": {"result": False, "reason": "car_wash"}})
    assert r["ok"] is False
    assert "car_wash" in r["reason"]


def test_interpret_strips_the_proxy_prefix():
    r = commands.interpret(200, {"response": {
        "result": False, "reason": "car could not execute command: vehicle is in park"}})
    assert r["reason"] == "vehicle is in park"


def test_interpret_unpaired_key_gets_an_actionable_message():
    r = commands.interpret(200, {"response": None,
                                 "error": "your public key has not been paired with the vehicle"})
    assert r["ok"] is False
    assert "pair" in r["message"].lower()


def test_interpret_408_is_asleep():
    r = commands.interpret(408, {})
    assert r["ok"] is False
    assert "asleep" in r["message"].lower()


def test_interpret_malformed_body_with_no_response_object():
    """`response` missing or not a dict, with no recognized error and a
    status outside the named cases (408/403/429) — the generic fallback
    branch. Previously only reachable indirectly through other tests."""
    r = commands.interpret(400, {"response": None, "error": ""})
    assert r["ok"] is False
    assert r["reason"] == "http_400"
    assert "400" in r["message"]
