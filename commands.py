"""Command catalog, validation, and result interpretation.

Three facts shape this module:
  * HTTP 200 does not mean the car did anything. Parse `response.result`.
  * `reason` is not a stable enum on the signed path — the proxy prefixes it
    with "car could not execute command: ". Strip, then treat as opaque text.
  * Several documented commands return 400 invalid_command through Tesla's own
    proxy. They are excluded rather than shipped broken.
"""
from __future__ import annotations

from typing import Any

GROUPS = ["Charging", "Climate", "Access & security", "More"]

# The car reporting "nothing to do" is a success from the user's point of view.
BENIGN_REASONS = {"already_set", "not_charging", "requested", "is_charging",
                  "complete", "already open", "already closed",
                  "already on", "already off", "already_max_range",
                  "already_standard"}

_PROXY_PREFIXES = ("car could not execute command: ",
                   "vcsec could not execute command: ")


def _cmd(cid, label, group, params=(), confirm=False, needs=(), risk="normal"):
    return {"id": cid, "label": label, "group": group, "params": list(params),
            "confirm": confirm, "needs": list(needs), "risk": risk}


def _int(name, label, lo=None, hi=None, default=None):
    return {"name": name, "type": "int", "label": label,
            "min": lo, "max": hi, "default": default}


def _bool(name, label, default=False):
    return {"name": name, "type": "bool", "label": label, "default": default}


def _enum(name, label, options, default=None):
    return {"name": name, "type": "enum", "label": label,
            "options": list(options), "default": default}


def _str(name, label, secret=False):
    """`secret` makes the UI render a password field. PINs have no default —
    a defaulted PIN would be worse than no feature."""
    return {"name": name, "type": "string", "label": label,
            "secret": secret, "default": None}


CATALOG: list[dict[str, Any]] = [
    # ---- Charging ----
    _cmd("charge_start", "Start charging", "Charging"),
    _cmd("charge_stop", "Stop charging", "Charging"),
    _cmd("set_charge_limit", "Set charge limit", "Charging",
         # Tesla accepts an out-of-range percent, returns success, and silently
         # no-ops. Bounding it here is the only real validation. No default:
         # a silently-defaulted charge limit would be worse than requiring
         # the caller to say what they mean (same reasoning as PINs below).
         [_int("percent", "Limit %", 50, 100)]),
    _cmd("set_charging_amps", "Set charging amps", "Charging",
         [_int("charging_amps", "Amps", 1, 48, 32)]),
    _cmd("charge_port_door_open", "Open charge port", "Charging"),
    _cmd("charge_port_door_close", "Close charge port", "Charging"),

    # ---- Climate ----
    _cmd("auto_conditioning_start", "Climate on", "Climate"),
    _cmd("auto_conditioning_stop", "Climate off", "Climate"),
    _cmd("set_temps", "Set temperature", "Climate",
         [{"name": "driver_temp", "type": "float", "label": "Driver °C",
           "min": 15, "max": 28, "default": 21},
          {"name": "passenger_temp", "type": "float", "label": "Passenger °C",
           "min": 15, "max": 28, "default": 21}]),
    _cmd("set_preconditioning_max", "Defrost (max)", "Climate",
         [_bool("on", "On", True)]),
    # seat_position is 0-BASED here and 1-based on the cooler/auto commands.
    # Off-by-one silently heats the wrong seat.
    _cmd("remote_seat_heater_request", "Seat heater", "Climate",
         [_enum("seat_position", "Seat",
                [{"value": 0, "label": "Front left"}, {"value": 1, "label": "Front right"},
                 {"value": 2, "label": "Rear left"}, {"value": 4, "label": "Rear center"},
                 {"value": 5, "label": "Rear right"}], 0),
          _int("level", "Level", 0, 3, 0)],
         needs=["climate_on"]),
    _cmd("remote_steering_wheel_heater_request", "Steering wheel heater", "Climate",
         [_bool("on", "On", True)], needs=["climate_on"]),
    _cmd("set_climate_keeper_mode", "Climate keeper", "Climate",
         [_enum("climate_keeper_mode", "Mode",
                [{"value": 0, "label": "Off"}, {"value": 1, "label": "Keep"},
                 {"value": 2, "label": "Dog"}, {"value": 3, "label": "Camp"}], 0)]),
    _cmd("set_cabin_overheat_protection", "Cabin overheat protection", "Climate",
         [_bool("on", "On", True), _bool("fan_only", "Fan only", False)]),

    # ---- Access & security ----
    _cmd("door_lock", "Lock", "Access & security"),
    _cmd("door_unlock", "Unlock", "Access & security", confirm=True, risk="high"),
    _cmd("actuate_trunk", "Open trunk", "Access & security",
         [_enum("which_trunk", "Which",
                [{"value": "rear", "label": "Rear"}, {"value": "front", "label": "Frunk"}],
                "rear")],
         confirm=True, risk="high"),
    _cmd("set_sentry_mode", "Sentry mode", "Access & security",
         [_bool("on", "On", True)]),
    _cmd("flash_lights", "Flash lights", "Access & security"),
    _cmd("honk_horn", "Honk horn", "Access & security", confirm=True),

    # ---- More ----
    # The proxy ignores lat/lon entirely, but the field reference documents them
    # as a proximity proof, so they are omitted rather than faked.
    _cmd("window_control", "Windows", "More",
         [_enum("command", "Action",
                [{"value": "vent", "label": "Vent"}, {"value": "close", "label": "Close"}],
                "close")],
         confirm=True, risk="high"),
    _cmd("set_valet_mode", "Valet mode", "More",
         [_bool("on", "On", True)], confirm=True, risk="high"),
    # Speed Limit Mode. Tesla documents no range for limit_mph and the proxy
    # does not validate it; 50-90 is the range the car's own UI offers.
    _cmd("speed_limit_activate", "Speed limit on", "More",
         [_str("pin", "4-digit PIN", secret=True)], confirm=True, risk="high"),
    _cmd("speed_limit_deactivate", "Speed limit off", "More",
         [_str("pin", "4-digit PIN", secret=True)], confirm=True),
    _cmd("speed_limit_set_limit", "Speed limit value", "More",
         [_int("limit_mph", "mph", 50, 90, 75)], confirm=True),
    _cmd("remote_start_drive", "Remote start", "More", confirm=True, risk="high"),
    _cmd("media_toggle_playback", "Play / pause", "More", needs=["user_present"]),
    _cmd("media_next_track", "Next track", "More", needs=["user_present"]),
    _cmd("media_prev_track", "Previous track", "More", needs=["user_present"]),
    _cmd("adjust_volume", "Volume", "More",
         [{"name": "volume", "type": "float", "label": "0-10",
           "min": 0, "max": 10, "default": 5}], needs=["user_present"]),
    _cmd("trigger_homelink", "HomeLink", "More", confirm=True),
    _cmd("schedule_software_update", "Install update", "More",
         [_int("offset_sec", "Delay (s)", 0, 86400, 0)], confirm=True),
    _cmd("cancel_software_update", "Cancel update", "More"),
]

_BY_ID = {c["id"]: c for c in CATALOG}


def find(cmd_id: str) -> dict[str, Any] | None:
    return _BY_ID.get(cmd_id)


def validate(spec: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce to real JSON types and bound-check.

    The proxy's getBool demands JSON true/false and getNumber a JSON number —
    {"on": "true"} and {"percent": "80"} are both rejected upstream, so the
    coercion has to happen here."""
    body: dict[str, Any] = {}
    for param in spec["params"]:
        name = param["name"]
        if name not in payload or payload[name] is None:
            if param.get("default") is None:
                raise ValueError(f"missing required parameter {name!r}")
            body[name] = param["default"]
            continue

        raw = payload[name]
        kind = param["type"]
        if kind == "bool":
            if isinstance(raw, bool):
                body[name] = raw
            elif str(raw).lower() in {"true", "1", "yes"}:
                body[name] = True
            elif str(raw).lower() in {"false", "0", "no"}:
                body[name] = False
            else:
                raise ValueError(f"{name} must be a boolean")
        elif kind in {"int", "float"}:
            try:
                value = int(raw) if kind == "int" else float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be a number") from None
            if param.get("min") is not None and value < param["min"]:
                raise ValueError(f"{name} must be at least {param['min']}")
            if param.get("max") is not None and value > param["max"]:
                raise ValueError(f"{name} must be at most {param['max']}")
            body[name] = value
        elif kind == "string":
            text = str(raw).strip()
            if not text:
                raise ValueError(f"{name} is required")
            body[name] = text
        elif kind == "enum":
            allowed = [o["value"] for o in param["options"]]
            value = raw
            if value not in allowed:
                # Enum values arrive as strings over HTTP even when they are ints.
                for option in allowed:
                    if str(option) == str(raw):
                        value = option
                        break
                else:
                    raise ValueError(f"{name} must be one of {allowed}")
            body[name] = value
        else:
            body[name] = raw
    return body


def interpret(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """Turn a proxy response into something a human can act on."""
    body = body or {}
    error = (body.get("error") or "").strip()

    if error:
        if "has not been paired" in error or "UNKNOWN_KEY_ID" in error:
            return {"ok": False, "reason": error,
                    "message": "The car has not been paired with this app's key. "
                               "Open tesla.com/_ak/tenxcious.com on your phone."}
        if error == "invalid_command":
            return {"ok": False, "reason": error,
                    "message": "The signing proxy does not support this command."}

    if status == 408:
        return {"ok": False, "reason": "asleep",
                "message": "The car is asleep. Wake it, then try again."}
    if status == 403:
        return {"ok": False, "reason": "forbidden",
                "message": "Not permitted — the token is missing a command scope."}
    if status == 429:
        return {"ok": False, "reason": "rate_limited",
                "message": "Rate limited by Tesla. These limits are shared with "
                           "every app authorized on this account."}

    response = body.get("response")
    if not isinstance(response, dict):
        return {"ok": False, "reason": error or f"http_{status}",
                "message": error or f"Unexpected response ({status})."}

    reason = (response.get("reason") or "").strip()
    for prefix in _PROXY_PREFIXES:
        if reason.startswith(prefix):
            reason = reason[len(prefix):]
            break

    if response.get("result") is True:
        return {"ok": True, "reason": reason, "message": "Done"}
    if reason in BENIGN_REASONS:
        return {"ok": True, "reason": reason, "message": reason.replace("_", " ").capitalize()}
    return {"ok": False, "reason": reason or "rejected",
            "message": f"The car declined: {reason or 'no reason given'}"}
