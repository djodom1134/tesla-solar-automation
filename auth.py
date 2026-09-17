"""Who may call this service.

Two independent checks, because they close two different holes.

TOKEN, for anything not on the loopback interface. The dashboard binds to the
LAN (commit 7d3c6a2), which means the garage opener and the config writes are
reachable from every device on the network. A shared secret is the whole
defence.

SEC-FETCH-SITE, on every mutating method REGARDLESS of source address. The
attack the token does not stop is a drive-by form on any web page the owner
visits, submitting to 127.0.0.1:8000 from the owner's own browser -- which
arrives on loopback and would be exempt. Browsers set Sec-Fetch-Site on every
request and it cannot be forged by script, so a value present and not
same-origin is a refusal. ABSENCE is not: curl, the collector and every
non-browser client omit it entirely.

The cookie exists so the LAN browser UI keeps working -- a page load cannot
send X-Api-Key. It is deliberately CSRF-able, and the Sec-Fetch-Site check
above is what makes that safe.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from http.cookies import SimpleCookie

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import settings

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# Control surfaces: a token is required even from loopback. These are not the
# local UI, and nothing legitimate reaches them from a browser page load.
GUARDED = ("/api/ha/", "/mcp")

# The OAuth dance lands the browser here with Tesla's code. A 401 would
# strand it with no way to finish authorising.
OPEN = ("/auth/",)


@dataclass(frozen=True)
class Incoming:
    """The parts of a request this module judges. A plain value so the
    decision is a pure function and testable without a live app."""
    path: str
    method: str
    client_host: str | None
    headers: dict = field(default_factory=dict)


def _presented(req: Incoming) -> str:
    header = req.headers.get("x-api-key")
    if header:
        return header
    raw = req.headers.get("cookie")
    if not raw:
        return ""
    try:
        jar = SimpleCookie()
        jar.load(raw)
    except Exception:
        return ""
    morsel = jar.get("api_token")
    return morsel.value if morsel else ""


def _token_ok(presented: str, tokens: tuple[str, ...]) -> bool:
    """Constant-time comparison against every configured token.

    An empty `presented` is refused BEFORE the loop. Without that, a
    deployment with only one of the three tokens configured would have the
    other two as empty strings, and an absent header would compare equal to
    them -- turning a partial configuration into no authentication at all.
    """
    if not presented:
        return False
    ok = False
    for token in tokens:
        if token and secrets.compare_digest(presented, token):
            ok = True
    return ok


def decide(req: Incoming, tokens: tuple[str, ...]) -> tuple[int, str] | None:
    """None to allow, or (status, detail) to refuse. Pure."""
    if req.method in MUTATING:
        site = req.headers.get("sec-fetch-site")
        if site is not None and site != "same-origin":
            return 403, "cross-site request refused"

    if req.path.startswith(OPEN):
        return None

    guarded = req.path.startswith(GUARDED)
    if not guarded and req.client_host in LOOPBACK:
        return None

    if _token_ok(_presented(req), tokens):
        return None
    return 401, "unauthorized"


def _tokens() -> tuple[str, ...]:
    return (settings.api_token, settings.api_token_ha, settings.api_token_mcp)


def install(app: FastAPI) -> None:
    """Wire the middleware and the cookie-setting route onto `app`."""

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        verdict = decide(
            Incoming(
                path=request.url.path,
                method=request.method,
                client_host=request.client.host if request.client else None,
                # Starlette headers are already lower-cased on lookup, but a
                # plain dict is what `decide` takes, so normalise here.
                headers={k.lower(): v for k, v in request.headers.items()},
            ),
            _tokens(),
        )
        if verdict is not None:
            status, detail = verdict
            return JSONResponse({"detail": detail}, status_code=status)
        return await call_next(request)

    @app.get("/auth/ui")
    async def _set_ui_cookie(token: str = ""):
        """Trade a token in the query string for a cookie, so the LAN browser
        UI works. Loopback browsers never need this."""
        if not _token_ok(token, _tokens()):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "api_token", token, httponly=True, samesite="strict", max_age=31536000)
        return response
