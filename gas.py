"""What the miles this car drove would have cost in gasoline.

The price is the EIA's weekly retail average for regular gasoline in Denver --
the nearest metro the EIA publishes for this site, and the only public source
that needs no account and no scraping of a commercial site. It is published
every Monday for that Monday's week; a daily fetch (collector.py) keeps the
local table current within a day of each release.

The history page carries every week back to the 1990s, so the whole series
is stored, not just the latest figure. Energy charged in August is priced at
August's gas, not today's: a savings total that moved every time the pump
price did would describe the pump, not the car.

Two savings, never blended, because they answer different questions:

  sun   -- miles put in from solar, priced as gallons NOT bought. The sun is
           taken as free, as the owner asked. It is not strictly: an exported
           kWh earns export credit, so a solar kWh in the car is a credit
           forgone. That is the owner's framing to change, not this module's.
  grid  -- miles put in from grid electricity, priced as gallons not bought,
           LESS what that electricity cost at the import rate. Negative is a
           real answer (very expensive power, very cheap gas) and is reported,
           not clamped.
"""
from __future__ import annotations

import bisect
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

import green

EIA_SERIES = "EMM_EPMR_PTE_YDEN_DPG"   # Denver, regular, all formulations
EIA_URL = ("https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx"
           f"?n=PET&s={EIA_SERIES}&f=W")
SOURCE = "EIA weekly retail, Denver regular"

# The owner's figure for the car this one replaces.
MPG = 20.0

REFRESH_S = 24 * 3600
# After a failed fetch, the next try waits this long rather than a whole day,
# and rather than every loop pass: the EIA being down must not cost a
# request every two minutes, nor a day's staleness.
RETRY_S = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS gas_prices (
  week        TEXT PRIMARY KEY,     -- ISO date of the EIA week (a Monday)
  usd_per_gal REAL NOT NULL,
  fetched_at  INTEGER NOT NULL
);
"""

_ROW_YEAR = re.compile(r"class='B6'>(?:&nbsp;|\s)*(\d{4})-[A-Za-z]{3}")
_ROW_WEEK = re.compile(
    r"class='B5'>(\d{2})/(\d{2})&nbsp;</td>\s*<td class='B3'>([\d.]+)")


def parse_eia_weekly(html: str) -> list[tuple[str, float]]:
    """(week, usd_per_gal) pairs from an EIA weekly history page, oldest
    first. Rows are one calendar month each, labelled `YYYY-Mon`, with up to
    five `MM/DD` / price cell pairs; empty trailing cells carry no digits and
    are skipped by the pattern itself."""
    out: list[tuple[str, float]] = []
    for row in html.split("<tr"):
        year = _ROW_YEAR.search(row)
        if not year:
            continue
        for month, day, price in _ROW_WEEK.findall(row):
            out.append((f"{year.group(1)}-{month}-{day}", float(price)))
    return sorted(out)


def save_prices(db: sqlite3.Connection, prices: list[tuple[str, float]],
                now: float) -> None:
    db.executemany(
        "INSERT OR REPLACE INTO gas_prices (week, usd_per_gal, fetched_at)"
        " VALUES (?, ?, ?)", [(w, p, int(now)) for w, p in prices])
    db.commit()


def load_prices(db: sqlite3.Connection) -> list[tuple[str, float]]:
    return [(r[0], r[1]) for r in db.execute(
        "SELECT week, usd_per_gal FROM gas_prices ORDER BY week")]


def last_fetched(db: sqlite3.Connection) -> int | None:
    row = db.execute("SELECT MAX(fetched_at) FROM gas_prices").fetchone()
    return row[0] if row else None


def due(db: sqlite3.Connection, now: float, last_attempt: float) -> bool:
    """Whether to fetch now: a day since the last success, and not within
    RETRY_S of the last attempt. Judged from the table, so a restarted
    collector does not refetch what it already has."""
    if now - last_attempt < RETRY_S:
        return False
    fetched = last_fetched(db)
    return fetched is None or now - fetched >= REFRESH_S


async def refresh(db: sqlite3.Connection) -> int:
    """Fetch the series and store it. Returns the number of weeks stored.
    Raises on any network or parse failure, so the caller can log it."""
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
        resp = await c.get(EIA_URL)
        resp.raise_for_status()
    prices = parse_eia_weekly(resp.text)
    if not prices:
        raise ValueError("EIA page parsed to no prices; layout changed?")
    save_prices(db, prices, time.time())
    return len(prices)


def price_for(prices: list[tuple[str, float]], day: str) -> float | None:
    """The price in effect on `day` (ISO): the latest week starting on or
    before it. A day before the first stored week takes the first week --
    the nearest honest figure, not a gap."""
    if not prices:
        return None
    chosen = prices[0][1]
    for week, price in prices:
        if week > day:
            break
        chosen = price
    return chosen


def savings(ticks: list[dict[str, Any]], prices: list[tuple[str, float]],
            mi_per_kwh: float | None, import_rate: float | None, tz: str,
            mpg: float = MPG) -> dict[str, Any] | None:
    """Money saved against a `mpg` gasoline car, sun and grid apart.

    `ticks` need ts, car_w, grid_w and period_s -- the same rows
    green.charged_split reads, split tick by tick the same way, so these
    totals always agree with the "Charged so far" kWh beside them.

    None without prices or without a measured mi/kWh: miles from an assumed
    pack size are exactly what the card refuses to show elsewhere. Grid
    figures are None without an import rate, since the electricity side of
    that saving is then unknown -- the sun side still stands.
    """
    if not prices or not mi_per_kwh:
        return None
    zone = ZoneInfo(tz)
    sun_kwh = grid_kwh = sun_usd = grid_gas_usd = 0.0
    for tick in ticks:
        car_w = float(tick.get("car_w") or 0)
        if car_w <= 0:
            continue
        grid_w = float(tick.get("grid_w") or 0)
        hours = float(tick.get("period_s") or 0) / 3600
        day = datetime.fromtimestamp(tick["ts"], zone).date().isoformat()
        per_kwh = mi_per_kwh / mpg * price_for(prices, day)
        s = green.tick_solar_w(car_w, grid_w) * hours / 1000
        g = green.tick_grid_w(car_w, grid_w) * hours / 1000
        sun_kwh += s
        grid_kwh += g
        sun_usd += s * per_kwh
        grid_gas_usd += g * per_kwh
    grid_elec_usd = grid_kwh * import_rate if import_rate is not None else None
    return {
        "sun_usd": round(sun_usd, 2),
        "sun_kwh": round(sun_kwh, 2),
        "grid_gas_usd": round(grid_gas_usd, 2),
        "grid_electric_usd": (round(grid_elec_usd, 2)
                              if grid_elec_usd is not None else None),
        "grid_usd": (round(grid_gas_usd - grid_elec_usd, 2)
                     if grid_elec_usd is not None else None),
        "grid_kwh": round(grid_kwh, 2),
    }


# --------------------------------------------------------------------------
# The whole life of the car, and a year of it going forward.
#
# Everything above is MEASURED: ticks the collector saw, priced at the gas of
# their week. Everything below is an ESTIMATE, and says so on the card. The
# odometer is exact; how those miles divide between sun and grid before this
# system existed is not, so the ratio measured since (free_miles_driven /
# tracked_miles -- miles actually driven on banked sun) stands in for the
# whole life. That ratio counts miles, not kWh charged at home, which is what
# makes it honest here: it includes energy from Superchargers and anywhere
# else the car charged, where the home-charging kWh split does not.
# --------------------------------------------------------------------------

# VIN position 10, 2010 onward. I, O, Q, U and Z are never used.
_MODEL_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY"


def model_year(vin: str | None) -> int | None:
    """The model year the VIN declares, or None if it cannot be read."""
    if not vin or len(vin) != 17:
        return None
    code = vin[9].upper()
    i = _MODEL_YEAR_CODES.find(code)
    return 2010 + i if i >= 0 else None


def in_service(configured_ts: int | None, vin: str | None,
               tz: str) -> tuple[float | None, str | None]:
    """(unix time the car entered service, basis). The owner's own date
    wins. Failing that, 1 July of the model year: a model year is sold from
    roughly the autumn before to the autumn of, so midyear is the estimate
    that is wrong by least either way -- and it is labelled an estimate."""
    if configured_ts:
        return float(configured_ts), "configured"
    year = model_year(vin)
    if year is None:
        return None, None
    return datetime(year, 7, 1, tzinfo=ZoneInfo(tz)).timestamp(), "model year"


def _price_on(weeks: list[str], values: list[float], day: str) -> float:
    i = bisect.bisect_right(weeks, day) - 1
    return values[max(i, 0)]


def projection(*, odometer_mi: float | None, in_service_ts: float | None,
               now: float, prices: list[tuple[str, float]],
               sun_share: float | None, mi_per_kwh: float | None,
               import_rate: float | None, tz: str,
               mpg: float = MPG) -> dict[str, Any] | None:
    """Lifetime money saved against a `mpg` gasoline car, and a year of it
    projected forward, each split sun / grid.

    Lifetime spreads the odometer evenly over the car's service life and
    prices each week's share at that week's gas -- a 2022 mile was a 2022
    gallon. The year ahead is the lifetime-average mileage at today's gas.

    Grid miles cost electricity at the home import rate over measured
    mi/kWh. Supercharging costs more than that, so the grid saving is an
    upper bound for any car that uses them -- stated on the card.
    """
    if (not prices or not odometer_mi or not in_service_ts
            or not mi_per_kwh or import_rate is None or now <= in_service_ts):
        return None
    sun = min(max(sun_share or 0.0, 0.0), 1.0)
    years = (now - in_service_ts) / (365.25 * 86400)
    annual_mi = odometer_mi / years
    elec_per_mi = import_rate / mi_per_kwh

    zone = ZoneInfo(tz)
    weeks = [w for w, _ in prices]
    values = [p for _, p in prices]
    start = datetime.fromtimestamp(in_service_ts, zone)
    end = datetime.fromtimestamp(now, zone)
    total_days = (end - start).total_seconds() / 86400
    gas_usd = 0.0          # what a gasoline car would have spent, lifetime
    day = start
    while day < end:
        step = min(timedelta(days=7), end - day)
        miles = odometer_mi * (step.total_seconds() / 86400) / total_days
        gas_usd += miles / mpg * _price_on(weeks, values, day.date().isoformat())
        day += step

    grid_mi = odometer_mi * (1 - sun)
    life_sun = gas_usd * sun
    life_grid = gas_usd * (1 - sun) - grid_mi * elec_per_mi
    gas_per_mi = values[-1] / mpg
    year_sun = annual_mi * sun * gas_per_mi
    year_grid = annual_mi * (1 - sun) * (gas_per_mi - elec_per_mi)
    return {
        "odometer_mi": round(odometer_mi, 1),
        "years": round(years, 2),
        "annual_mi": round(annual_mi),
        "sun_share": round(sun, 4),
        "electric_usd_per_mi": round(elec_per_mi, 4),
        "lifetime_sun_usd": round(life_sun, 2),
        "lifetime_grid_usd": round(life_grid, 2),
        "lifetime_usd": round(life_sun + life_grid, 2),
        "yearly_sun_usd": round(year_sun, 2),
        "yearly_grid_usd": round(year_grid, 2),
        "yearly_usd": round(year_sun + year_grid, 2),
    }
