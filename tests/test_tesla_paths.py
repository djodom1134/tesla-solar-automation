from tesla import VEHICLE_ENDPOINTS, endpoints_param


def test_endpoints_are_semicolon_joined():
    assert endpoints_param(["charge_state", "drive_state"]) == "charge_state;drive_state"


def test_endpoints_default_covers_everything_the_page_needs():
    for group in ("charge_state", "climate_state", "drive_state",
                  "location_data", "vehicle_state", "gui_settings"):
        assert group in VEHICLE_ENDPOINTS


def test_endpoints_param_rejects_commas():
    # A caller passing a pre-joined comma string is the classic mistake.
    try:
        endpoints_param(["charge_state,drive_state"])
    except ValueError:
        return
    raise AssertionError("expected ValueError for a comma-containing group")
