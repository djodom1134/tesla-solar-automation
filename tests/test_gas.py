from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

import gas

# The tail of the real EIA page, 2026-09-18, markup verbatim: month rows of
# `MM/DD` / price pairs, blank trailing cells, then the footnote table.
EIA_TAIL = """<tr> <td class='B6'>&nbsp;&nbsp;2026-Aug</td> <td class='B5'>08/03&nbsp;</td>
<td class='B3'>4.082&nbsp;&nbsp;&nbsp;</td> <td class='B5'>08/10&nbsp;</td>
<td class='B3'>4.041&nbsp;&nbsp;&nbsp;</td> <td class='B5'>08/17&nbsp;</td>
<td class='B3'>4.274&nbsp;&nbsp;&nbsp;</td> <td class='B5'>08/24&nbsp;</td>
<td class='B3'>4.430&nbsp;&nbsp;&nbsp;</td> <td class='B5'>08/31&nbsp;</td>
<td class='B3'>4.164&nbsp;&nbsp;&nbsp;</td> </tr> <tr> <td class='B6'>&nbsp;&nbsp;2026-Sep</td>
<td class='B5'>09/07&nbsp;</td> <td class='B3'>4.152&nbsp;&nbsp;&nbsp;</td>
<td class='B5'>09/14&nbsp;</td> <td class='B3'>4.250&nbsp;&nbsp;&nbsp;</td>
<td class='B5'>&nbsp;</td> <td class='B3'>&nbsp;&nbsp;&nbsp;</td> </tr> </tbody> </table>
<table width='675'> <tr> <td class='F2'>Release Date: 9/15/2026</td> </tr> </table>"""

TZ = "America/Denver"


def _ts(day: str, hour: int = 12) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00")
               .replace(tzinfo=ZoneInfo(TZ)).timestamp())


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(gas.SCHEMA)
    return db


def test_parses_every_week_and_skips_the_blank_cells():
    prices = gas.parse_eia_weekly(EIA_TAIL)
    assert prices[0] == ("2026-08-03", 4.082)
    assert prices[-1] == ("2026-09-14", 4.25)
    assert len(prices) == 7


def test_a_page_with_no_table_parses_to_nothing():
    assert gas.parse_eia_weekly("<html>Service unavailable</html>") == []


def test_the_price_in_effect_is_the_latest_week_on_or_before_the_day():
    prices = gas.parse_eia_weekly(EIA_TAIL)
    assert gas.price_for(prices, "2026-09-14") == 4.25     # the Monday itself
    assert gas.price_for(prices, "2026-09-13") == 4.152    # the Sunday before
    assert gas.price_for(prices, "2026-09-18") == 4.25     # not yet re-released
    assert gas.price_for(prices, "2026-07-01") == 4.082    # before the series
    assert gas.price_for([], "2026-09-18") is None


def test_sun_is_priced_as_gas_not_bought_and_grid_nets_off_its_power():
    """10 kWh of sun and 10 kWh of grid at 3 mi/kWh against 20 mpg: 30 miles
    is 1.5 gal each. Grid then pays 10 kWh at $0.12."""
    prices = [("2026-09-14", 4.00)]
    ticks = [
        # 5 kW into the car, site exporting 1 kW: all sun, for 2 h.
        {"ts": _ts("2026-09-15"), "car_w": 5000, "grid_w": -1000,
         "period_s": 7200},
        # 5 kW into the car, site importing 6 kW: all grid, for 2 h.
        {"ts": _ts("2026-09-15", 20), "car_w": 5000, "grid_w": 6000,
         "period_s": 7200},
    ]
    s = gas.savings(ticks, prices, 3.0, 0.12, TZ)
    assert s["sun_kwh"] == 10 and s["grid_kwh"] == 10
    assert s["sun_usd"] == pytest.approx(6.00)
    assert s["grid_gas_usd"] == pytest.approx(6.00)
    assert s["grid_electric_usd"] == pytest.approx(1.20)
    assert s["grid_usd"] == pytest.approx(4.80)


def test_each_charge_is_priced_at_its_own_weeks_gas():
    prices = [("2026-09-07", 4.00), ("2026-09-14", 5.00)]
    tick = {"car_w": 5000, "grid_w": -1000, "period_s": 7200}   # 10 kWh sun
    s = gas.savings([{**tick, "ts": _ts("2026-09-08")},
                     {**tick, "ts": _ts("2026-09-15")}], prices, 2.0, 0.1, TZ)
    # 20 mi each = 1 gal each, at $4 then $5.
    assert s["sun_usd"] == pytest.approx(9.00)


def test_no_import_rate_leaves_the_grid_saving_unknown_but_not_the_sun():
    s = gas.savings([{"ts": _ts("2026-09-15"), "car_w": 5000,
                      "grid_w": 6000, "period_s": 3600}],
                    [("2026-09-14", 4.0)], 3.0, None, TZ)
    assert s["grid_usd"] is None and s["grid_electric_usd"] is None
    assert s["grid_gas_usd"] > 0


def test_expensive_power_is_a_negative_saving_not_a_clamped_one():
    s = gas.savings([{"ts": _ts("2026-09-15"), "car_w": 5000,
                      "grid_w": 6000, "period_s": 7200}],
                    [("2026-09-14", 1.00)], 3.0, 1.00, TZ)
    assert s["grid_usd"] < 0


def test_no_measured_mileage_or_no_prices_means_no_figure_at_all():
    tick = [{"ts": _ts("2026-09-15"), "car_w": 5000, "grid_w": 0,
             "period_s": 3600}]
    assert gas.savings(tick, [("2026-09-14", 4.0)], None, 0.12, TZ) is None
    assert gas.savings(tick, [], 3.0, 0.12, TZ) is None


def test_fetches_daily_and_backs_off_an_hour_after_a_failure():
    db = _db()
    now = 1_800_000_000.0
    assert gas.due(db, now, last_attempt=0)                 # never fetched
    assert not gas.due(db, now, last_attempt=now - 60)      # just failed
    assert gas.due(db, now, last_attempt=now - gas.RETRY_S)
    gas.save_prices(db, [("2026-09-14", 4.25)], now)
    assert not gas.due(db, now + 3600, last_attempt=0)      # fresh
    assert gas.due(db, now + gas.REFRESH_S, last_attempt=0)  # a day on


@pytest.mark.asyncio
async def test_refresh_stores_the_series_and_refuses_an_unparseable_page(
        monkeypatch):
    pages = iter([EIA_TAIL, "<html>maintenance</html>"])
    real = httpx.AsyncClient

    def fake_client(**kw):
        return real(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, text=next(pages))), **kw)
    monkeypatch.setattr(gas.httpx, "AsyncClient", fake_client)

    db = _db()
    assert await gas.refresh(db) == 7
    assert gas.load_prices(db)[-1] == ("2026-09-14", 4.25)
    with pytest.raises(ValueError):
        await gas.refresh(db)
    assert len(gas.load_prices(db)) == 7, "a bad page must not wipe the table"


# ------------------------------------------------------------ projection

def test_model_year_reads_the_tenth_vin_character():
    assert gas.model_year("5YJSA0000N0000000") == 2022   # N
    assert gas.model_year("5YJSA0000P0000000") == 2023   # P (O is skipped)
    assert gas.model_year("5YJSA0000A0000000") == 2010
    assert gas.model_year("5YJSA0000Z0000000") is None   # never used
    assert gas.model_year("short") is None
    assert gas.model_year(None) is None


def test_in_service_prefers_the_owners_date_then_midyear_of_the_model_year():
    assert gas.in_service(1_650_000_000, "5YJSA0000N0000000", TZ) == (
        1_650_000_000.0, "configured")
    ts, basis = gas.in_service(None, "5YJSA0000N0000000", TZ)
    assert basis == "model year"
    assert datetime.fromtimestamp(ts, ZoneInfo(TZ)).date().isoformat() == "2022-07-01"
    assert gas.in_service(None, None, TZ) == (None, None)


def _project(**over):
    start = _ts("2022-09-19", 0)
    base = dict(odometer_mi=40_000.0, in_service_ts=start,
                now=start + 4 * 365.25 * 86400, prices=[("2000-01-03", 4.00)],
                sun_share=0.25, mi_per_kwh=4.0, import_rate=0.12, tz=TZ)
    return gas.projection(**{**base, **over})


def test_projection_splits_a_whole_life_and_a_year_ahead_by_the_sun_share():
    """40,000 mi over 4 yr at a flat $4.00 and 20 mpg is 2,000 gal = $8,000
    of gas. A quarter of the miles were sun: $2,000 saved outright. The rest
    cost $0.03/mi of power (0.12 / 4 mi/kWh): 30,000 mi is $900 against $6,000
    of gas. The year ahead is the same car at 10,000 mi/yr."""
    p = _project()
    assert p["annual_mi"] == 10_000
    assert p["electric_usd_per_mi"] == pytest.approx(0.03)
    assert p["lifetime_sun_usd"] == pytest.approx(2000, abs=1)
    assert p["lifetime_grid_usd"] == pytest.approx(5100, abs=1)
    assert p["lifetime_usd"] == pytest.approx(7100, abs=2)
    assert p["yearly_sun_usd"] == pytest.approx(500)
    assert p["yearly_grid_usd"] == pytest.approx(1275)
    assert p["yearly_usd"] == pytest.approx(1775)


def test_lifetime_miles_are_priced_at_the_gas_of_their_own_week():
    """Two years at $3, two at $5: lifetime gas averages $4, but the year
    ahead is priced at today's $5."""
    p = _project(prices=[("2000-01-03", 3.00), ("2024-09-16", 5.00)],
                 sun_share=1.0)
    assert p["lifetime_sun_usd"] == pytest.approx(8000, rel=0.01)
    assert p["yearly_sun_usd"] == pytest.approx(2500)


def test_no_projection_without_the_facts_it_rests_on():
    assert _project(odometer_mi=None) is None
    assert _project(in_service_ts=None) is None
    assert _project(mi_per_kwh=None) is None
    assert _project(import_rate=None) is None
    assert _project(prices=[]) is None


def test_an_unknown_sun_share_counts_every_mile_as_grid():
    p = _project(sun_share=None)
    assert p["sun_share"] == 0 and p["lifetime_sun_usd"] == 0
