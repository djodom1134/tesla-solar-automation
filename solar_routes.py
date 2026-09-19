"""HTTP surface for home configuration and the solar controller.

This module never commands the car. The collector owns every write to the
vehicle; the web app only reads status and edits configuration, which the
collector picks up on its next tick. That keeps exactly one process issuing
commands and makes the signing proxy's per-VIN mutex a non-issue.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, HTTPException

import demo
import garage
import gas
import green
import home
import landmarks
import solar
from config import settings
from store import Store

DEMO = os.getenv("DEMO", "").strip() in {"1", "true", "yes"}

router = APIRouter(prefix="/api/car")

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(settings.db_file)
    return _store


def _vin() -> str:
    """The VIN the collector is recording under.

    Prefers the configured one; otherwise the most recent sample. Returns ""
    when nothing has ever been recorded, which every caller tolerates.
    """
    if settings.vin:
        return settings.vin
    row = store()._db.execute(
        "SELECT vin FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    return row["vin"] if row else ""


def _today() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")


def _midnight_ts() -> int:
    now = datetime.now(ZoneInfo(settings.timezone))
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _solar_ticks(db, vin: str, since_ts: int) -> list[dict[str, Any]]:
    """Rows for green.solar_kwh(). Same since_ts convention as
    solar.grace_import_wh() -- 0 for all time, _midnight_ts() for today."""
    if not vin:
        return []
    rows = db.execute(
        "SELECT state, car_w, grid_w, period_s FROM solar_ticks"
        " WHERE vin = ? AND ts >= ?", (vin, since_ts),
    ).fetchall()
    return [dict(row) for row in rows]


def _sessions_and_segments(
    db, vin: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Charge sessions and driving segments, both derived from the same
    ordered sample history and both bounded by the same charging-run edges.

    A charging run's own first and last row mark the exact moment charging
    started or stopped, so that row doubles as the boundary anchor for the
    *adjacent* driving segment too. Anchoring a segment at the next-available
    sample instead -- rather than at the charging run's edge -- would
    silently drop whatever the car did in between: often hours and several
    miles, since the collector polls slowly once it has no reason to hurry
    (see collector.next_interval). Using the charging edges as shared anchors
    means every mile and every percent of SoC in the whole history lands in
    exactly one bucket: a charge session or a driving segment, never neither.
    """
    if not vin:
        return [], []
    rows = db.execute(
        "SELECT battery_level, charging, charge_energy_added, odometer"
        " FROM samples WHERE vin = ? ORDER BY ts", (vin,),
    ).fetchall()
    n = len(rows)
    if n == 0:
        return [], []

    charging_runs: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if rows[i]["charging"]:
            j = i
            while j < n and rows[j]["charging"]:
                j += 1
            charging_runs.append((i, j - 1))
            i = j
        else:
            i += 1

    sessions = []
    for start, end in charging_runs:
        first, last = rows[start], rows[end]
        added_start, added_end = first["charge_energy_added"], last["charge_energy_added"]
        if added_start is None or added_end is None:
            continue  # charge_energy_added missing for this run -- not usable
        sessions.append({
            "soc_start": first["battery_level"], "soc_end": last["battery_level"],
            "kwh_added": added_end - added_start,
        })

    anchors = [0]
    for start, end in charging_runs:
        anchors += [start, end]
    anchors.append(n - 1)

    segments = []
    for k in range(0, len(anchors) - 1, 2):
        start_idx, end_idx = anchors[k], anchors[k + 1]
        if start_idx == end_idx:
            continue
        first, last = rows[start_idx], rows[end_idx]
        if first["odometer"] is None or last["odometer"] is None:
            continue
        if first["battery_level"] is None or last["battery_level"] is None:
            continue
        segments.append({
            "miles": last["odometer"] - first["odometer"],
            "soc_drop": first["battery_level"] - last["battery_level"],
        })

    return sessions, segments


def _green_status(
    db, vin: str, solar_soc: float, soc: int | None, range_mi: float | None,
    free_miles_driven: float, tracked_miles: float, free_miles_since: int | None,
) -> dict[str, Any]:
    """The free-miles answer, the daily FLOW (Task 16), the banked STOCK
    (Task 18) and the lifetime free miles actually driven (Task 20), honest
    about what each doesn't know yet.

    mi_per_kwh and pack_kwh are lifetime figures -- they only get more
    trustworthy with more history, so there is no reason to reset them
    daily. solar_kwh_today is scoped to the calendar day, same convention as
    grace_import_wh_today. banked_pct/banked_miles are NOT scoped to the
    day -- the bank is a running balance, not a daily tally, so it persists
    across midnight exactly like the pack itself. Neither are
    free_miles_driven/tracked_miles -- same running-balance treatment,
    accumulated tick by tick in collector.py (green.free_miles_step), never
    recomputed here from history.

    banked_miles is the measured figure when pack_kwh/mi_per_kwh have
    cleared their thresholds, else the car's own rated-range figure, never
    both blended together -- banked_miles_basis says which so the UI can
    label it rather than switch silently (spec 7.4).

    free_miles_share is None (not 0) while tracked_miles is still 0 -- 0/0
    is undefined, not a real zero, and the UI must say "not tracked yet"
    rather than a misleading 0%.
    """
    solar_today = green.solar_kwh(_solar_ticks(db, vin, _midnight_ts()))
    sessions, segments = _sessions_and_segments(db, vin)
    pack, pack_n = green.pack_kwh(sessions)
    mpk, miles = green.miles_per_kwh(segments, pack)
    free = green.free_miles(solar_today, mpk)
    # One resolver for every energy-to-miles conversion on this card.
    _mpk, _mpk_basis = green.effective_mi_per_kwh(mpk, soc, range_mi, pack)

    # Lifetime energy into the car, split by origin -- DERIVED from the tick
    # log, never accumulated into a counter. A counter starts at zero on the
    # day it is added, so it disagrees with every history-derived figure
    # beside it; that is exactly how "charged so far" came to contradict
    # "banked solar" on the card.
    # EVERY tick the car drew, not only the ones the controller was driving:
    # scoping this to engaged ticks omits charges the owner started at full
    # rate from the grid, which read 61% solar against a true 29% on this
    # site. The banked ledger counts those sessions (grid dilutes the bank),
    # so anything shown beside it must count them too.
    charged_solar, charged_grid = green.charged_split(_solar_ticks(db, vin, 0))
    charged_total = charged_solar + charged_grid

    rated = green.banked_miles_rated(solar_soc, soc, range_mi)
    measured = green.banked_miles_measured(solar_soc, pack, mpk)
    if measured is not None:
        banked_miles, banked_basis = measured, "measured"
    elif rated is not None:
        banked_miles, banked_basis = rated, "rated"
    else:
        banked_miles, banked_basis = None, None

    free_share = (round(100 * free_miles_driven / tracked_miles, 1)
                 if tracked_miles > 0 else None)

    return {
        "free_miles": round(free, 1) if free is not None else None,
        "solar_kwh_today": round(solar_today, 2),
        "mi_per_kwh": round(mpk, 2) if mpk is not None else None,
        "miles_sampled": round(miles, 1),
        "pack_kwh": round(pack, 1) if pack is not None else None,
        "pack_sessions": pack_n,
        "banked_pct": round(solar_soc, 2),
        "banked_miles_rated": round(rated, 1) if rated is not None else None,
        "banked_miles_measured": round(measured, 1) if measured is not None else None,
        "banked_miles": round(banked_miles, 1) if banked_miles is not None else None,
        "banked_miles_basis": banked_basis,
        "free_miles_driven": round(free_miles_driven, 1),
        "tracked_miles": round(tracked_miles, 1),
        "free_miles_share": free_share,
        "free_miles_since": free_miles_since,
        # Lifetime energy INTO the car, split by origin. Reported in kWh,
        # which is MEASURED, and converted to miles only when mi/kWh has been
        # measured too.
        #
        # Not merely a labelling nicety: banked_miles is solar_soc/soc x the
        # car's own rated range and needs no pack size at all, while
        # kWh -> miles goes through mi/kWh = rated_range / pack. With pack
        # unknown, NOMINAL_PACK_KWH stands in, and the two figures disagree by
        # whatever that guess is wrong by -- on this car the ledger implies
        # ~69 kWh against an assumed 100, so the same energy read as 7.2 miles
        # in one line and 10.4 in the other. Showing a miles figure that rests
        # on a guess, beside one that does not, is what made the card
        # contradict itself.
        "charged_solar_kwh": round(charged_solar, 2),
        "charged_grid_kwh": round(charged_grid, 2),
        "charged_solar_miles": (
            round(charged_solar * mpk, 1) if mpk else None),
        "charged_grid_miles": (
            round(charged_grid * mpk, 1) if mpk else None),
        "charged_solar_share": (
            round(100.0 * charged_solar / charged_total, 1)
            if charged_total > 0 else None),
        # "measured" only. Deliberately NOT _mpk_basis, which falls back to
        # the rated/nominal-pack estimate -- see above.
        "charged_miles_basis": "measured" if mpk else None,
    }


@router.get("/home")
async def get_home() -> dict[str, Any]:
    if DEMO:
        return demo.home_config()

    db = store()._db
    cfg = home.load(db)
    vin = _vin()
    snap = store().snapshot(vin) if vin else None
    view = (snap or {}).get("view") or {}
    car = None
    if view.get("lat") is not None and view.get("lon") is not None:
        car = {"lat": view["lat"], "lon": view["lon"]}
    return {
        "home": None if cfg is None else {
            "latitude": cfg.latitude, "longitude": cfg.longitude,
            "radius_m": cfg.radius_m,
        },
        "classification": home.classify(view, cfg),
        "car": car,
    }


@router.put("/home")
async def put_home(body: dict[str, Any] = Body(...)) -> dict[str, bool]:
    try:
        lat = float(body["latitude"])
        lon = float(body["longitude"])
        radius = int(body["radius_m"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "latitude, longitude and radius_m are required")
    if not -90 <= lat <= 90:
        raise HTTPException(400, "latitude must be between -90 and 90")
    if not -180 <= lon <= 180:
        raise HTTPException(400, "longitude must be between -180 and 180")
    if not 10 <= radius <= 2000:
        raise HTTPException(400, "radius_m must be between 10 and 2000")
    home.save(store()._db, lat, lon, radius)
    return {"ok": True}


# Bounds are validation, not taste. period_s has a hard floor because
# grid_power only refreshes every 60 s; anything faster re-reads one value.
CONFIG_BOUNDS = {
    "enabled": (0, 1), "period_s": (60, 900), "margin_w": (0, 2000),
    "deadband_w": (50, 2000), "ramp_a": (1, 48), "min_a": (5, 32),
    # restart_hold_s may be 0: with the wake skipped for an online
    # car, an awake restart costs one $0.001 command, and making it
    # wait discards surplus to insure against a cost not incurred.
    "grace_s": (0, 3600), "restart_hold_s": (0, 3600),
    "start_hold_s": (0, 3600), "raise_hold_s": (0, 7200),
    "soc_ceiling": (50, 100), "raise_limit": (0, 1),
    "daily_request_cap": (0, 20000), "view_refresh_ticks": (1, 60),
    "deadline_soc": (0, 100),
    "deadline_hour": (0, 23),
    "grace_budget_wh": (0, 5000),
    # Tariff, in currency units per kWh. The upper bounds are sanity
    # rails, not economics -- they exist so a fat-fingered 12 (dollars)
    # instead of 0.12 is rejected rather than silently reported as a
    # hundred-fold cost.
    "import_rate": (0, 5), "export_rate": (0, 5),
    # Floor of 60 s: the grid meter itself only refreshes that often,
    # so anything faster re-reads one value at double the billing.
    "watch_s": (60, 3600),
    # The manual-override pause. 1 by default -- see solar.override_step.
    "pause_on_override": (0, 1),
    # Unix time the car entered service, for its lifetime mileage average.
    # From 2008 (the first Roadster) to 2100: a sanity rail, as above.
    "in_service_ts": (1_199_145_600, 4_102_444_800),
}

# Fields carrying real values rather than counts. int() would silently
# floor an $0.12 tariff to $0.
FLOAT_CONFIG_FIELDS = ("import_rate", "export_rate", "grace_budget_wh")

# Fields where absence is meaningful and must round-trip as NULL.
NULLABLE_CONFIG_FIELDS = ("deadline_soc", "deadline_hour",
                          "import_rate", "export_rate", "in_service_ts")


@router.get("/solar/config")
async def get_solar_config() -> dict[str, Any]:
    return solar.load_config(store()._db)


@router.put("/solar/config")
async def put_solar_config(body: dict[str, Any] = Body(...)) -> dict[str, bool]:
    unknown = set(body) - set(CONFIG_BOUNDS)
    if unknown:
        raise HTTPException(400, f"unknown fields: {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for key, value in body.items():
        if value is None and key in NULLABLE_CONFIG_FIELDS:
            clean[key] = None
            continue
        want_float = key in FLOAT_CONFIG_FIELDS
        try:
            number = float(value) if want_float else int(value)
        except (TypeError, ValueError):
            raise HTTPException(
                400, f"{key} must be a {'number' if want_float else 'integer'}")
        low, high = CONFIG_BOUNDS[key]
        if not low <= number <= high:
            raise HTTPException(400, f"{key} must be between {low} and {high}")
        clean[key] = number

    # Switching solar charging back ON is the owner saying, in the plainest
    # available words, that they want this controller driving again -- so it
    # releases a manual-override pause exactly as the car page's button does.
    # Without this, an owner who paused, turned the feature off, and later
    # turned it back on would land straight back in "manual" with no visible
    # reason and the controller still standing aside.
    #
    # Only on a real 0 -> 1 TRANSITION, never on `enabled` merely being 1 in
    # the body: the setup page posts the whole form on every save, so an
    # unrelated tuning change would otherwise cancel a pause the owner never
    # mentioned.
    was_enabled = bool(solar.load_config(store()._db)["enabled"])
    solar.save_config(store()._db, **clean)
    vin = _vin()
    if vin and clean.get("enabled") == 1 and not was_enabled:
        solar.save_state(store()._db, vin, override_amps=None,
                         override_since=None, override_armed=0,
                         commanded_amps=None, commanded_ack=0)
    return {"ok": True}


CHARGE_MODES = ("solar", "now", "off")


def _mode_payload(conf: dict, st: dict | None, now: float) -> dict[str, Any]:
    return {
        "mode": solar.charge_mode(conf, st, now),
        # Only meaningful while forcing. Reported as null otherwise rather
        # than as a stale timestamp the caller has to interpret.
        "expires_ts": (conf["force_charge_until"]
                       if solar.forcing(conf, now) else None),
        "enabled": bool(conf["enabled"]),
    }


def _mode_state() -> dict:
    """The per-vehicle state the mode is derived from, or the defaults when
    there is no vehicle row yet (a fresh install)."""
    vin = _vin()
    return solar.load_state(store()._db, vin) if vin else dict(solar.STATE_DEFAULTS)


@router.get("/charge-mode")
async def get_charge_mode() -> dict[str, Any]:
    return _mode_payload(solar.load_config(store()._db), _mode_state(),
                         time.time())


@router.put("/charge-mode")
async def put_charge_mode(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Set the charge mode.

    force_charge_until is deliberately NOT in CONFIG_BOUNDS, so it cannot be
    written through PUT /solar/config. A client that could set the timestamp
    directly could set it a year out, and the midnight expiry -- the whole
    safety property of "now" -- would be gone. This route is the only way in,
    and it computes the expiry itself.
    """
    mode = body.get("mode")
    if mode not in CHARGE_MODES:
        raise HTTPException(400, f"mode must be one of {list(CHARGE_MODES)}")

    now = time.time()
    if mode == "now":
        solar.save_config(
            store()._db,
            force_charge_until=solar.next_midnight_ts(settings.timezone, now))
    elif mode == "solar":
        solar.save_config(store()._db, force_charge_until=None, enabled=1)
    else:
        solar.save_config(store()._db, force_charge_until=None, enabled=0)

    # Every route through here is the owner stating, explicitly, who should
    # be driving -- which is exactly what the manual-override pause was
    # waiting to hear. This is the "or automated control is re-enabled from
    # the car page" half of the release condition, and it is deliberately
    # not limited to mode="solar": choosing "now" or "off" settles the
    # question just as squarely, and leaving a stale latch behind would make
    # the mode flip back to "manual" the moment the force expired.
    #
    # The whole handshake is cleared with it, not just the latch. Keeping a
    # commanded_amps from before the override would let the very first tick
    # after the resume compare the owner's rate against a stale command and
    # latch all over again.
    vin = _vin()
    if vin:
        solar.save_state(store()._db, vin, override_amps=None,
                         override_since=None, override_armed=0,
                         commanded_amps=None, commanded_ack=0)

    return _mode_payload(solar.load_config(store()._db), _mode_state(), now)


def _savings(db, vin: str, conf: dict, mi_per_kwh: float | None) -> dict | None:
    """Money saved against a gasoline car -- see gas.py. Lifetime figures
    cover the same ticks as charged_split (all of them), and today's sun
    figure the same day as free_miles, so every number on the card is taken
    over the ground the line beside it describes."""
    prices = gas.load_prices(db)
    if not vin or not prices:
        return None
    rows = [dict(r) for r in db.execute(
        "SELECT ts, car_w, grid_w, period_s FROM solar_ticks"
        " WHERE vin = ? AND car_w > 0", (vin,))]
    rate = conf.get("import_rate")
    if rate is None:
        rate = settings.import_rate
    lifetime = gas.savings(rows, prices, mi_per_kwh, rate, settings.timezone)
    if lifetime is None:
        return None
    midnight = _midnight_ts()
    today = gas.savings([r for r in rows if r["ts"] >= midnight], prices,
                        mi_per_kwh, rate, settings.timezone)
    week, price = prices[-1]
    return {**lifetime,
            "sun_usd_today": today["sun_usd"] if today else 0.0,
            "gas_usd_per_gal": price, "gas_week": week,
            "gas_source": gas.SOURCE, "mpg": gas.MPG,
            "import_rate": rate}


def _projection(db, vin: str, conf: dict, view: dict,
                green_status: dict) -> dict | None:
    """Lifetime and yearly money saved against a gasoline car -- see
    gas.projection. The sun share is miles actually driven on banked sun,
    falling back to the home-charging kWh split only before any driving has
    been tracked."""
    prices = gas.load_prices(db)
    if not vin or not prices:
        return None
    share = green_status["free_miles_share"]
    if share is None:
        share = green_status["charged_solar_share"]
    started, basis = gas.in_service(conf.get("in_service_ts"),
                                    view.get("vin") or vin, settings.timezone)
    rate = conf.get("import_rate")
    if rate is None:
        rate = settings.import_rate
    out = gas.projection(
        odometer_mi=view.get("odometer_mi"), in_service_ts=started,
        now=time.time(), prices=prices,
        sun_share=(share / 100) if share is not None else None,
        mi_per_kwh=green_status["mi_per_kwh"], import_rate=rate,
        tz=settings.timezone)
    if out is None:
        return None
    return {**out, "in_service_ts": int(started), "in_service_basis": basis,
            "sun_share_basis": ("driven" if green_status["free_miles_share"]
                                is not None else "charged")}


@router.get("/solar/status")
async def get_solar_status() -> dict[str, Any]:
    if DEMO:
        return demo.solar_status()

    db = store()._db
    vin = _vin()
    state = solar.load_state(db, vin) if vin else dict(solar.STATE_DEFAULTS)
    snap = store().snapshot(vin) if vin else None
    view = (snap or {}).get("view") or {}
    last = db.execute(
        "SELECT ts, surplus_w, amps_written, amps_before, car_w, grid_w, solar_w"
        " FROM solar_ticks"
        " WHERE vin = ? ORDER BY ts DESC LIMIT 1", (vin,)).fetchone() if vin else None

    # How fast banked free miles are growing, so the page can animate
    # between polls instead of jumping once a tick. Only the SOLAR part of
    # the draw counts: a car riding out a cloud at the floor is charging,
    # but on grid electrons, and must not tick the counter up.
    accrual, accrual_basis = 0.0, "none"
    if last is not None and state["state"] == "charging":
        car_w = float(last["car_w"] or 0)
        grid_w = float(last["grid_w"] or 0)
        free_w = max(0.0, car_w - max(0.0, grid_w))
        _g = _green_status(
            db, vin, state["solar_soc"], view.get("soc"),
            view.get("range_mi"), state["free_miles_driven"],
            state["tracked_miles"], state["free_miles_since"])
        accrual, accrual_basis = green.accrual_mi_per_s(
            free_w, _g["mi_per_kwh"], view.get("soc"),
            view.get("range_mi"), _g["pack_kwh"])
    conf = solar.load_config(db)
    green_status = _green_status(
        db, vin, state["solar_soc"], view.get("soc"), view.get("range_mi"),
        state["free_miles_driven"], state["tracked_miles"],
        state["free_miles_since"])
    return {
        "state": state["state"],
        "enabled": bool(conf["enabled"]),
        # Which of the four things is driving the car, as one word. `state`
        # is the solar machine's own position and cannot answer this: it
        # reads "idle" both when the controller is waiting for sun and when
        # it has stood aside because the owner set their own rate.
        "mode": solar.charge_mode(conf, state, time.time()),
        # Only meaningful in "manual". The rate the OWNER set, and when --
        # the card says what happened rather than just that something did.
        "override_amps": state["override_amps"],
        "override_since": state["override_since"],
        "surplus_w": last["surplus_w"] if last else None,
        "accrual_mi_per_s": round(accrual, 8),
        "accrual_basis": accrual_basis,
        "as_of": int(time.time()),
        "amps": (last["amps_written"] or last["amps_before"]) if last else None,
        "soc": view.get("soc"),
        "limit": view.get("limit"),
        "raised_to": state["raised_to"],
        "original_limit": state["original_limit"],
        "grace_import_wh_today": round(
            solar.grace_import_wh(db, vin, _midnight_ts()), 1) if vin else 0,
        "grace_import_wh_total": round(
            solar.grace_import_wh(db, vin, 0), 1) if vin else 0,
        "capped": bool(state["capped"]),
        "dirty": bool(state["dirty"]),
        # Invariant 4 (spec 3.7): a sustained 429 means the controller is
        # polling slower than the owner's configured period thinks -- surface
        # it the same way capped/dirty are, not just in the collector's log.
        "rate_limited": bool(state["consecutive_429s"]),
        "backoff_s": state["backoff_s"],
        "engaged_at": state["engaged_at"],
        "last_tick_ts": last["ts"] if last else None,
        "requests_today": state["requests_today"] if state["requests_day"] == _today() else 0,
        # Task 18: a lower bound when true -- a gap longer than
        # store.GAP_SECONDS since the ledger last observed the car means the
        # pack may have changed unobserved.
        "ledger_stale": bool(state["ledger_stale"]),
        **green_status,
        # WHY it is not charging, in the site's own numbers. "Waiting for
        # spare sun" alone cannot tell a house eating 11.5 kW (2026-09-19
        # 14:34, the oven and the car plugged in at the same minute) from a
        # controller that has stopped working, and the owner had no way to
        # tell either. house_w is the site's own identity, grid = house + car
        # - solar, rearranged; it is what the meter says, not a new reading.
        "solar_w": last["solar_w"] if last else None,
        "grid_w": last["grid_w"] if last else None,
        "house_w": (
            (last["solar_w"] or 0) + (last["grid_w"] or 0) - (last["car_w"] or 0)
            if last and last["solar_w"] is not None and last["grid_w"] is not None
            else None),
        # The surplus a charge needs before it can start, so the card can say
        # how far off it is rather than just that it is waiting.
        "start_w": solar.start_watts(solar.tunables_from(
            conf, view.get("amps_max"), view.get("volts"))),
        "sun_wasted": solar.sun_wasted(
            enabled=bool(conf["enabled"]),
            plugged=view.get("charging_state") not in (None, "Disconnected"),
            location=home.classify(view, home.load(db)),
            soc=view.get("soc"),
            ceiling=conf["soc_ceiling"],
            state=state["state"],
            recent=solar.recent_ticks(db, vin, 40) if vin else [],
            start_w=solar.start_watts(solar.tunables_from(
                conf, view.get("amps_max"), view.get("volts"))),
            min_s=900,
            now=time.time()),
        "savings": _savings(db, vin, conf, green_status["mi_per_kwh"]),
        "projection": _projection(db, vin, conf, view, green_status),
    }


# ---------------------------------------------------------------- garage (Task 17b)
#
# Direct LAN calls to the ratgdo, never through the Tesla signing proxy --
# the "this module never commands the car" invariant above is about the
# vehicle, not this device. garage.status()/open()/close() are synchronous
# (httpx.Client); every call here goes through asyncio.to_thread so a slow
# or unreachable device cannot stall the single-threaded event loop -- the
# exact mistake a sibling module made with a blocking socket.

GARAGE_CONFIG_FIELDS = ("garage_url", "garage_auto_open", "garage_ring_m",
                        "garage_close_hour", "garage_close_warn_s")


async def _garage_reading(url: str) -> dict[str, Any]:
    data = await asyncio.to_thread(garage.status, url)
    if data is None:
        return {"reachable": False}
    return {
        "reachable": True,
        "door_state": data.get("garageDoorState"),
        "obstructed": bool(data.get("garageObstructed")),
        "light_on": bool(data.get("garageLightOn")),
    }


@router.get("/garage")
async def get_garage() -> dict[str, Any]:
    """Live door state for the car page's manual controls. Never a stale
    value dressed up as current: an unconfigured or unreachable device
    reports reachable: false rather than the last thing we happened to see."""
    if DEMO:
        return demo.garage_status()
    url = solar.load_config(store()._db)["garage_url"]
    if not url:
        return {"reachable": False}
    return await _garage_reading(url)


@router.post("/garage/open")
async def post_garage_open() -> dict[str, Any]:
    """Manual button. The owner is present and just pressed it, so this acts
    immediately -- the warned-close discipline in collector.py's scheduled
    close applies only to the *unattended* automatic close, never to a
    button the owner is standing in front of."""
    if DEMO:
        return {"ok": True, **demo.garage_status()}
    url = solar.load_config(store()._db)["garage_url"]
    if not url:
        raise HTTPException(400, "garage URL is not configured")
    commanded = await asyncio.to_thread(garage.open, url)
    return {"ok": commanded, **await _garage_reading(url)}


@router.post("/garage/close")
async def post_garage_close() -> dict[str, Any]:
    """Manual button -- same immediacy as open() above; see its docstring."""
    if DEMO:
        return {"ok": True, **demo.garage_status()}
    url = solar.load_config(store()._db)["garage_url"]
    if not url:
        raise HTTPException(400, "garage URL is not configured")
    commanded = await asyncio.to_thread(garage.close, url)
    return {"ok": commanded, **await _garage_reading(url)}


@router.post("/garage/test")
async def post_garage_test(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Setup page's "Test connection" -- reads status against whatever URL is
    currently typed in, saved or not. Never DEMO-branched and never writes:
    the ratgdo is real hardware on the LAN regardless of whether the rest of
    this app is running against demo Tesla data."""
    url = str(body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    return await _garage_reading(url)


@router.get("/garage/config")
async def get_garage_config() -> dict[str, Any]:
    cfg = solar.load_config(store()._db)
    return {k: cfg[k] for k in GARAGE_CONFIG_FIELDS}


@router.put("/garage/config")
async def put_garage_config(body: dict[str, Any] = Body(...)) -> dict[str, bool]:
    unknown = set(body) - set(GARAGE_CONFIG_FIELDS)
    if unknown:
        raise HTTPException(400, f"unknown fields: {sorted(unknown)}")
    clean: dict[str, Any] = {}

    if "garage_url" in body:
        url = body["garage_url"]
        if url is not None:
            url = str(url).strip()
            if url and not (url.startswith("http://") or url.startswith("https://")):
                raise HTTPException(400, "garage_url must start with http:// or https://")
        clean["garage_url"] = url or None

    if "garage_auto_open" in body:
        try:
            v = int(body["garage_auto_open"])
        except (TypeError, ValueError):
            raise HTTPException(400, "garage_auto_open must be 0 or 1")
        if v not in (0, 1):
            raise HTTPException(400, "garage_auto_open must be 0 or 1")
        clean["garage_auto_open"] = v

    if "garage_ring_m" in body:
        try:
            v = int(body["garage_ring_m"])
        except (TypeError, ValueError):
            raise HTTPException(400, "garage_ring_m must be an integer")
        if not 50 <= v <= 5000:
            raise HTTPException(400, "garage_ring_m must be between 50 and 5000")
        clean["garage_ring_m"] = v

    if "garage_close_hour" in body:
        raw = body["garage_close_hour"]
        if raw is None:
            clean["garage_close_hour"] = None
        else:
            try:
                v = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(400, "garage_close_hour must be an integer or null")
            if not 0 <= v <= 23:
                raise HTTPException(400, "garage_close_hour must be between 0 and 23")
            clean["garage_close_hour"] = v

    if "garage_close_warn_s" in body:
        try:
            v = int(body["garage_close_warn_s"])
        except (TypeError, ValueError):
            raise HTTPException(400, "garage_close_warn_s must be an integer")
        if not 5 <= v <= 60:
            raise HTTPException(400, "garage_close_warn_s must be between 5 and 60")
        clean["garage_close_warn_s"] = v

    solar.save_config(store()._db, **clean)
    return {"ok": True}


@router.get("/solar/landmarks")
async def get_landmarks() -> dict[str, Any]:
    """Places reachable on banked sunshine, nearest first.

    The WHOLE list is returned with a per-place threshold, not just what
    currently fits: the page animates banked miles upward continuously
    between polls, and this lets places light up as the number climbs
    without another request.
    """
    if DEMO:
        return demo.landmarks()
    db = store()._db
    vin = _vin()
    state = solar.load_state(db, vin) if vin else dict(solar.STATE_DEFAULTS)
    snap = store().snapshot(vin) if vin else None
    view = (snap or {}).get("view") or {}
    g = _green_status(db, vin, state["solar_soc"], view.get("soc"),
                      view.get("range_mi"), state["free_miles_driven"],
                      state["tracked_miles"], state["free_miles_since"])
    return {
        "banked_miles": g["banked_miles"],
        "round_trip": True,
        "places": landmarks.reachable(home.load(db), g["banked_miles"]),
    }
