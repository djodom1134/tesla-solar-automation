"""Tesla solar import/export dashboard.

    python app.py     ->  http://localhost:8000
"""
from __future__ import annotations

import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import auth
import car_routes
import demo
import energy
import ha_routes
import solar
import solar_routes
from config import BASE_DIR, settings
from tesla import TeslaAPIError, TeslaAuthError, TeslaClient, day_bounds, period_end

# DEMO=1 serves synthetic data through the identical derivation path — no Tesla account needed.
DEMO = os.getenv("DEMO", "").strip() in {"1", "true", "yes"}

client = TeslaClient(settings)

# Our label -> the Tesla `period` value and the bucket size it returns.
PERIODS: dict[str, dict[str, str]] = {
    "today": {"tesla": "day", "bucket": "day", "label": "Today"},
    "week": {"tesla": "week", "bucket": "day", "label": "This week"},
    "month": {"tesla": "month", "bucket": "day", "label": "This month"},
    "year": {"tesla": "year", "bucket": "month", "label": "This year"},
    "lifetime": {"tesla": "lifetime", "bucket": "year", "label": "Lifetime"},
}

# OAuth CSRF states, kept in memory: state -> created_at.
_pending_states: dict[str, float] = {}
_STATE_TTL = 600


def _new_state() -> str:
    now = time.time()
    for key, created in list(_pending_states.items()):
        if now - created > _STATE_TTL:
            _pending_states.pop(key, None)
    state = secrets.token_urlsafe(24)
    _pending_states[state] = now
    return state


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await client.aclose()


app = FastAPI(title="Tesla Energy Dashboard", lifespan=lifespan)
auth.install(app)


@app.exception_handler(TeslaAuthError)
async def _auth_error(_: Request, exc: TeslaAuthError) -> JSONResponse:
    return JSONResponse({"error": "auth", "detail": str(exc)}, status_code=401)


@app.exception_handler(TeslaAPIError)
async def _api_error(_: Request, exc: TeslaAPIError) -> JSONResponse:
    return JSONResponse({"error": "upstream", "detail": str(exc)}, status_code=502)


# ---------- auth ----------


@app.get("/auth/login")
async def login() -> RedirectResponse:
    if not settings.configured:
        raise HTTPException(500, "TESLA_CLIENT_ID / TESLA_CLIENT_SECRET are not set. See SETUP.md.")
    return RedirectResponse(client.authorize_url(_new_state()))


@app.get("/auth/callback")
async def callback(
    code: str | None = None, state: str | None = None, error: str | None = None
) -> RedirectResponse:
    if error:
        return RedirectResponse(f"/?error={error}")
    if not code:
        return RedirectResponse("/?error=missing_code")
    if not state or state not in _pending_states:
        return RedirectResponse("/?error=bad_state")
    _pending_states.pop(state, None)
    await client.exchange_code(code)
    return RedirectResponse("/")


@app.get("/auth/manual", response_class=HTMLResponse)
async def manual_form() -> str:
    """For when the redirect URI is a domain you host rather than this local server:
    Tesla lands the browser there, and you paste the resulting URL back here."""
    return """<!doctype html><meta charset=utf-8><title>Paste callback URL</title>
    <style>body{font:16px system-ui;max-width:44rem;margin:4rem auto;padding:0 1rem}
    input{width:100%;padding:.6rem;font:inherit;margin:.75rem 0}
    button{padding:.6rem 1.2rem;font:inherit}</style>
    <h1>Paste the callback URL</h1>
    <p>After authorizing, your browser landed on your redirect URI with a <code>?code=</code>
    parameter. Copy that whole URL from the address bar and paste it below.</p>
    <form method=post><input name=url autofocus placeholder="https://your-domain.com/callback?code=..."
    ><button>Finish login</button></form>"""


@app.post("/auth/manual")
async def manual_submit(request: Request) -> RedirectResponse:
    form = await request.form()
    raw = str(form.get("url", ""))
    code = parse_qs(urlparse(raw).query).get("code", [None])[0]
    if not code:
        return RedirectResponse("/?error=no_code_in_url", status_code=303)
    await client.exchange_code(code)
    return RedirectResponse("/", status_code=303)


@app.post("/api/logout")
async def logout() -> dict[str, bool]:
    client.logout()
    return {"ok": True}


# ---------- data ----------


@app.get("/api/config")
async def api_config() -> dict[str, Any]:
    return {
        "configured": settings.configured or DEMO,
        "authenticated": client.authenticated or DEMO,
        "demo": DEMO,
        "timezone": settings.timezone,
        "currency": settings.currency,
        "has_rates": any(r is not None for r in _rates()),
        "periods": [{"key": k, "label": v["label"]} for k, v in PERIODS.items()],
        "manual_callback": not _is_local(settings.redirect_uri),
    }


def _rates() -> tuple[float | None, float | None]:
    """The tariff, preferring the database over .env.

    Rates moved into solar_config so they are editable from the setup page
    without a restart or a file edit; the .env values remain as a fallback so
    an existing deployment keeps working. A configured 0 is a real answer
    (some tariffs credit nothing for export) and must not fall through to the
    environment, so the test is "is not None", never truthiness.
    """
    try:
        cfg = solar.load_config(solar_routes.store()._db)
    except Exception:               # DB not ready; .env is all we have
        return settings.import_rate, settings.export_rate
    imp = cfg.get("import_rate")
    exp = cfg.get("export_rate")
    return (imp if imp is not None else settings.import_rate,
            exp if exp is not None else settings.export_rate)


def _is_local(uri: str) -> bool:
    host = (urlparse(uri).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


@app.get("/api/sites")
async def api_sites() -> dict[str, Any]:
    return {"sites": demo.sites() if DEMO else await client.energy_sites()}


async def _resolve_site(site_id: str | None) -> str:
    if site_id:
        return site_id
    sites = demo.sites() if DEMO else await client.energy_sites()
    if not sites:
        raise HTTPException(404, "No energy sites on this Tesla account.")
    return str(sites[0]["energy_site_id"])


@app.get("/api/dashboard")
async def api_dashboard(
    site_id: str | None = None, period: str = Query("today")
) -> dict[str, Any]:
    """Everything one view needs, in a single round trip."""
    if period not in PERIODS:
        raise HTTPException(400, f"period must be one of {sorted(PERIODS)}")

    site = await _resolve_site(site_id)
    spec = PERIODS[period]
    tz = settings.timezone
    zone = ZoneInfo(tz)
    now = datetime.now(zone)

    if DEMO:
        history = demo.calendar_history(spec["tesla"], tz)
    else:
        history = await client.calendar_history(
            site, spec["tesla"], period_end(spec["tesla"], tz, now), tz
        )
    raw_rows = (history or {}).get("time_series") or []
    rows = [energy.derive(r) for r in raw_rows]
    total = energy.summarize(rows)

    # These two are best-effort: a solar-only site with no gateway may not serve live_status,
    # and `kind=power` is not available on every account.
    live: dict[str, Any] | None = None
    try:
        status = demo.live_status(tz) if DEMO else await client.live_status(site)
        live = energy.live(status)
    except (TeslaAPIError, KeyError, TypeError):
        live = None

    intraday: list[dict[str, Any]] | None = None
    if period == "today":
        try:
            if DEMO:
                power = demo.power_history(tz)
            else:
                start, end = day_bounds(tz, now)
                power = await client.power_history(site, start, end, tz)
            intraday = energy.power_series((power or {}).get("time_series") or [])
        except (TeslaAPIError, KeyError, TypeError):
            intraday = None

    info: dict[str, Any] = {}
    try:
        info = (demo.site_info() if DEMO else await client.site_info(site)) or {}
    except TeslaAPIError:
        info = {}
    components = info.get("components") or {}

    return {
        "site_id": site,
        "site_name": info.get("site_name") or "Solar",
        "period": period,
        "bucket": spec["bucket"],
        "generated_at": now.isoformat(timespec="seconds"),
        "has_battery": bool(components.get("battery")),
        "has_solar": bool(components.get("solar", True)),
        "rows": rows,
        "total": total,
        "money": energy.money(total, *_rates()),
        "live": live,
        "intraday": intraday,
    }


app.include_router(car_routes.router)
app.include_router(solar_routes.router)
# Above the static mount, like the others -- that mount is a catch-all.
app.include_router(ha_routes.router)

class _NoStoreStatic(StaticFiles):
    """Serve the app's own files with no-store.

    ES modules are cached far more aggressively than plain scripts, and the
    cache key ignores the query string on the importing PAGE -- so a hard
    reload of car.html can still execute a stale car.js. Twice in this
    project that produced the same baffling symptom: the server and the disk
    byte-identical and correct, the page running code from an earlier deploy.
    Once it rendered a card with no live counter; once it threw
    "does not provide an export named ..." for an export that plainly existed.

    This is a single-user dashboard on localhost, so there is nothing to gain
    from caching and a whole class of ghost bugs to lose. Vendored assets
    under /vendor keep their normal caching -- they change when the file
    changes, which is approximately never.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if not path.startswith("vendor/"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


# The static mount is a catch-all: any router included after this line is
# unreachable. car_routes must be included above it.
app.mount("/", _NoStoreStatic(directory=BASE_DIR / "static", html=True),
          name="static")


if __name__ == "__main__":
    print(f"\n  Tesla energy dashboard -> http://{settings.host}:{settings.port}\n")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
