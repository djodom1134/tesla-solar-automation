import json
from pathlib import Path

import pytest

import vehicle

FIXTURE = Path(__file__).parent / "fixtures" / "vehicle_data.json"


@pytest.fixture
def raw():
    return json.loads(FIXTURE.read_text())


def test_derive_produces_the_documented_shape(raw):
    v = vehicle.derive(raw)
    for key in ("vin", "name", "soc", "usable_soc", "limit", "charging",
                "charging_state", "plugged_in", "range_mi", "odometer_mi",
                "locked", "doors", "windows", "sentry", "dashcam",
                "inside_c", "outside_c", "climate_on", "lat", "lon",
                "shift", "speed_mph", "tpms_bar", "software", "sampled_at"):
        assert key in v, f"missing {key}"


def test_charging_is_derived_from_state_not_voltage(raw):
    raw["charge_state"]["charging_state"] = "Disconnected"
    raw["charge_state"]["charger_voltage"] = 2  # the idle sentinel
    assert vehicle.derive(raw)["charging"] is False

    raw["charge_state"]["charging_state"] = "Charging"
    assert vehicle.derive(raw)["charging"] is True

    raw["charge_state"]["charging_state"] = "Starting"
    assert vehicle.derive(raw)["charging"] is True


def test_climate_uses_user_intent_not_any_reason(raw):
    raw["climate_state"]["is_climate_on"] = True
    raw["climate_state"]["is_auto_conditioning_on"] = False
    assert vehicle.derive(raw)["climate_on"] is False


def test_doors_and_windows_do_not_cross_axes(raw):
    raw["vehicle_state"].update(
        {"df": 1, "dr": 0, "pf": 0, "pr": 0,
         "fd_window": 0, "fp_window": 0, "rd_window": 1, "rp_window": 0}
    )
    v = vehicle.derive(raw)
    assert v["doors"] == {"driver_front": True, "driver_rear": False,
                          "passenger_front": False, "passenger_rear": False}
    assert v["windows"] == {"front_driver": False, "front_passenger": False,
                            "rear_driver": True, "rear_passenger": False}


def test_parked_car_reports_no_speed_and_parked_shift(raw):
    raw["drive_state"]["shift_state"] = None
    raw["drive_state"]["speed"] = None
    v = vehicle.derive(raw)
    assert v["shift"] == "P"
    assert v["speed_mph"] == 0


def test_invalid_sentinel_becomes_none_but_off_survives(raw):
    raw["charge_state"]["fast_charger_type"] = "<invalid>"
    raw["charge_state"]["charge_port_color"] = "Off"
    v = vehicle.derive(raw)
    assert v["fast_charger"] is None
    assert v["port_color"] == "Off"


def test_missing_location_keys_are_tolerated(raw):
    for k in ("latitude", "longitude", "heading", "gps_as_of"):
        raw["drive_state"].pop(k, None)
    v = vehicle.derive(raw)
    assert v["lat"] is None and v["lon"] is None


def test_idle_software_update_is_reported_as_none(raw):
    raw["vehicle_state"]["software_update"] = {
        "status": "", "version": " ", "download_perc": 0, "install_perc": 1,
    }
    assert vehicle.derive(raw)["software"] is None


def test_empty_payload_does_not_raise():
    v = vehicle.derive({})
    assert v["soc"] is None
    assert v["doors"] == {}


def test_derive_surfaces_the_fields_the_solar_loop_needs():
    import json
    from pathlib import Path
    import vehicle
    raw = json.loads((Path(__file__).parent / "fixtures" / "vehicle_data.json").read_text())
    view = vehicle.derive(raw)
    for key in ("volts", "fast_charger_present", "homelink_nearby", "homelink_devices"):
        assert key in view, f"{key} missing from derive()"
    assert view["homelink_devices"] == 2


def test_volts_is_none_when_idle_because_the_sensor_reads_two():
    # derive() consumes the raw vehicle_data dict directly (no "response"
    # wrapper) -- tesla.py already unwraps that before calling derive(), and
    # the `raw` fixture above has charge_state at the top level too.
    import vehicle
    view = vehicle.derive({
        "charge_state": {"charging_state": "Disconnected", "charger_voltage": 2},
        "climate_state": {}, "drive_state": {}, "vehicle_state": {},
        "gui_settings": {}, "vehicle_config": {}})
    assert view["volts"] is None


def test_volts_is_reported_while_charging():
    import vehicle
    view = vehicle.derive({
        "charge_state": {"charging_state": "Charging", "charger_voltage": 241},
        "climate_state": {}, "drive_state": {}, "vehicle_state": {},
        "gui_settings": {}, "vehicle_config": {}})
    assert view["volts"] == 241
