"""Tesla Fleet API client: OAuth third-party tokens, token rotation, energy endpoints."""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from config import AUTH_BASE, SCOPES, TOKEN_URL, Settings


class TeslaAuthError(RuntimeError):
    """Raised when we have no usable token and the user must log in again."""


class TeslaAPIError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Fleet API returned {status}: {body[:400]}")
        self.status = status
        self.body = body


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds

    @property
    def expired(self) -> bool:
        # Refresh a minute early so an in-flight request never races the expiry.
        return time.time() >= self.expires_at - 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_response(cls, data: dict[str, Any], previous: "Tokens | None" = None) -> "Tokens":
        # A refresh response occasionally omits refresh_token; keep the old one if so.
        refresh = data.get("refresh_token") or (previous.refresh_token if previous else "")
        return cls(
            access_token=data["access_token"],
            refresh_token=refresh,
            expires_at=time.time() + float(data.get("expires_in", 28800)),
        )


class TokenStore:
    """Persists tokens to disk.

    Tesla's refresh tokens are single-use and rotate on every exchange. If we lose the
    new one, the user has to re-authorize from scratch, so the write is atomic
    (write-temp-then-rename) and happens before the new access token is ever used.
    """

    def __init__(self, path: Path):
        self.path = path
        self._tokens: Tokens | None = None
        self._loaded = False

    def load(self) -> Tokens | None:
        if not self._loaded:
            try:
                data = json.loads(self.path.read_text())
                self._tokens = Tokens(**data)
            except (FileNotFoundError, json.JSONDecodeError, TypeError):
                self._tokens = None
            self._loaded = True
        return self._tokens

    def save(self, tokens: Tokens) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tokens.to_dict(), indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)  # atomic on POSIX
        self._tokens = tokens
        self._loaded = True

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        self._tokens = None
        self._loaded = True


class _Cache:
    """Tiny TTL cache. Fleet API bills per request, so we don't re-ask for data we just got."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._entries.get(key)
        if hit and time.time() < hit[0]:
            return hit[1]
        return None

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._entries[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        self._entries.clear()


class TeslaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = TokenStore(settings.token_file)
        self.cache = _Cache()
        self._http = httpx.AsyncClient(timeout=30.0)
        self._refresh_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------- OAuth ----------

    def authorize_url(self, state: str) -> str:
        params = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "scope": " ".join(SCOPES),
                "state": state,
                "prompt": "login",
            }
        )
        return f"{AUTH_BASE}/oauth2/v3/authorize?{params}"

    async def exchange_code(self, code: str) -> Tokens:
        resp = await self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "code": code,
                "audience": self.settings.audience,
                "redirect_uri": self.settings.redirect_uri,
            },
        )
        if resp.status_code != 200:
            raise TeslaAuthError(
                f"Code exchange failed ({resp.status_code}): {resp.text[:300]}. "
                "An 'invalid_auth_code' usually means the code expired — start the login again."
            )
        tokens = Tokens.from_response(resp.json())
        self.store.save(tokens)
        self.cache.clear()
        return tokens

    async def _refresh(self, current: Tokens) -> Tokens:
        resp = await self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self.settings.client_id,
                "refresh_token": current.refresh_token,
            },
        )
        if resp.status_code == 401:
            self.store.clear()
            raise TeslaAuthError(
                "Refresh token rejected. It either expired (they last 3 months), was superseded "
                "by a newer one, or the Tesla account password changed. Log in again."
            )
        if resp.status_code != 200:
            raise TeslaAuthError(f"Token refresh failed ({resp.status_code}): {resp.text[:300]}")
        tokens = Tokens.from_response(resp.json(), previous=current)
        self.store.save(tokens)  # persist BEFORE use — the old refresh token is now spent
        return tokens

    async def _access_token(self) -> str:
        tokens = self.store.load()
        if tokens is None:
            raise TeslaAuthError("Not logged in.")
        if not tokens.expired:
            return tokens.access_token

        async with self._refresh_lock:
            # Another coroutine may have refreshed while we waited for the lock.
            tokens = self.store.load()
            if tokens is None:
                raise TeslaAuthError("Not logged in.")
            if not tokens.expired:
                return tokens.access_token
            return (await self._refresh(tokens)).access_token

    @property
    def authenticated(self) -> bool:
        return self.store.load() is not None

    def logout(self) -> None:
        self.store.clear()
        self.cache.clear()

    # ---------- Fleet API ----------

    async def _get(self, path: str, params: dict[str, Any] | None = None, ttl: float = 0) -> Any:
        key = f"{path}?{sorted((params or {}).items())}"
        if ttl:
            cached = self.cache.get(key)
            if cached is not None:
                return cached

        url = f"{self.settings.api_base}{path}"
        for attempt in range(2):
            token = await self._access_token()
            resp = await self._http.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 401 and attempt == 0:
                # Access token rejected despite not looking expired — force one refresh, retry once.
                current = self.store.load()
                if current is None:
                    raise TeslaAuthError("Not logged in.")
                async with self._refresh_lock:
                    await self._refresh(current)
                continue
            if resp.status_code != 200:
                raise TeslaAPIError(resp.status_code, resp.text)
            payload = resp.json().get("response")
            if ttl:
                self.cache.set(key, payload, ttl)
            return payload
        raise TeslaAuthError("Could not authenticate to Fleet API.")

    async def products(self) -> list[dict[str, Any]]:
        return await self._get("/api/1/products", ttl=300) or []

    async def energy_sites(self) -> list[dict[str, Any]]:
        """Energy products only. Vehicles come back on the same endpoint; filter them out."""
        sites = []
        for product in await self.products():
            site_id = product.get("energy_site_id")
            if not site_id:
                continue
            sites.append(
                {
                    "energy_site_id": site_id,
                    "site_name": product.get("site_name") or f"Site {site_id}",
                    "resource_type": product.get("resource_type"),
                    "components": product.get("components", {}),
                }
            )
        return sites

    async def site_info(self, site_id: int | str) -> dict[str, Any]:
        return await self._get(f"/api/1/energy_sites/{site_id}/site_info", ttl=600)

    async def live_status(self, site_id: int | str) -> dict[str, Any]:
        return await self._get(f"/api/1/energy_sites/{site_id}/live_status", ttl=15)

    async def calendar_history(
        self, site_id: int | str, period: str, end: datetime, tz: str
    ) -> dict[str, Any]:
        return await self._get(
            f"/api/1/energy_sites/{site_id}/calendar_history",
            params={
                "kind": "energy",
                "period": period,
                "end_date": _iso(end),
                "time_zone": tz,
            },
            ttl=120,
        )

    async def power_history(
        self, site_id: int | str, start: datetime, end: datetime, tz: str
    ) -> dict[str, Any]:
        """Intraday power samples (watts). Not in every account's feature set — callers
        must tolerate this raising TeslaAPIError."""
        return await self._get(
            f"/api/1/energy_sites/{site_id}/calendar_history",
            params={
                "kind": "power",
                "start_date": _iso(start),
                "end_date": _iso(end),
                "time_zone": tz,
            },
            ttl=120,
        )


def _iso(dt: datetime) -> str:
    """Tesla wants an offset-aware ISO 8601 timestamp.

    Never pass midnight as end_date: the API returns all-zero energy values for a
    range that ends at 00:00:00. Callers use end-of-day instead.
    """
    return dt.isoformat(timespec="seconds")


def period_end(period: str, tz: str, now: datetime | None = None) -> datetime:
    """End-of-day anchor for a period. `period` is the Tesla bucket, not our label."""
    zone = ZoneInfo(tz)
    now = now or datetime.now(zone)
    return now.replace(hour=23, minute=59, second=59, microsecond=0)


def day_bounds(tz: str, day: datetime | None = None) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz)
    day = day or datetime.now(zone)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(hours=23, minutes=59, seconds=59)
