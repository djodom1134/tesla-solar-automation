"""Tesla Fleet API client: OAuth third-party tokens, token rotation, energy endpoints."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
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


class VehicleAsleep(RuntimeError):
    """HTTP 408 — the car is asleep or offline. Not an error path; the normal
    resting state. The response has no body at all."""


# Every group the car page reads. `location_data` is what actually yields
# coordinates; the `vehicle_location` scope alone is not enough.
VEHICLE_ENDPOINTS = [
    "charge_state",
    "climate_state",
    "drive_state",
    "location_data",
    "vehicle_state",
    "vehicle_config",
    "gui_settings",
]


def endpoints_param(groups: list[str]) -> str:
    """Tesla wants semicolons. Commas silently return a partial payload."""
    for g in groups:
        if "," in g:
            raise ValueError(f"endpoint group {g!r} contains a comma; pass a list instead")
    return ";".join(groups)


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


def _acquire_lock(token_path: Path) -> int:
    """Take an exclusive cross-process lock guarding token refresh.

    Locks a sidecar `.lock` file, never the token file itself: TokenStore.save()
    uses os.replace(), which swaps the inode, and an flock follows the inode —
    so a lock taken on the token file would be silently released mid-write.
    Blocking, so callers on an event loop must acquire via asyncio.to_thread.

    Test-only entry point (blocking). Production async code uses
    `_acquire_lock_async`, which never lets an fd survive an `await` while
    unlocked, so a cancelled task can't leak a held lock.
    """
    lock_path = token_path.with_suffix(".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)  # closing alone also releases the flock


async def _acquire_lock_async(token_path: Path, timeout: float = 60.0) -> int:
    """Cancellation-safe cross-process lock acquisition.

    A non-blocking retry loop rather than `asyncio.to_thread(_acquire_lock, ...)`:
    if the awaiting task were cancelled while a worker thread sat blocked in a
    blocking `flock`, the CancelledError would propagate before the fd is bound
    and before a `try` is entered, so the thread would go on to acquire the
    lock with nobody left holding (or able to release) the fd — wedging every
    other process out until this one exits. Here, no fd ever survives an
    `await` while unlocked: each iteration opens, tries LOCK_NB, and closes
    immediately on failure before the next `await asyncio.sleep`.
    """
    lock_path = token_path.with_suffix(".lock")
    deadline = time.monotonic() + timeout
    while True:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)  # fd never survives an await
            if time.monotonic() >= deadline:
                raise TeslaAuthError("Timed out waiting for the token refresh lock.")
            await asyncio.sleep(0.05)
        except BaseException:
            os.close(fd)
            raise


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

    def reload(self) -> Tokens | None:
        """Force a read from disk. Another process may have refreshed since we
        last looked, and its token is the only valid one."""
        self._loaded = False
        return self.load()

    def save(self, tokens: Tokens) -> None:
        # A new grant invalidates the previous refresh token. If a re-auth
        # half-fails we want the old grant on disk to fall back to.
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
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
        self._proxy: httpx.AsyncClient | None = None
        self._refresh_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._proxy is not None:
            await self._proxy.aclose()

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
                # Without this, an account that already consented to the old
                # scope set is silently re-issued a token missing the new ones.
                "prompt_missing_scopes": "true",
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

    async def _locked_refresh(self, stale: str | None = None) -> Tokens:
        """The ONLY path permitted to call _refresh.

        Holds an inter-process lock and re-reads from disk after acquiring it,
        because the process that held the lock before us may have already
        refreshed — in which case its token is valid and ours is spent.

        `stale` is the access token that just got rejected by the API, if any.
        A fresh-on-disk token only counts as "someone else already refreshed"
        when it differs from `stale` — otherwise this is the same token that
        just failed, and it must actually be sent through `_refresh`, not
        handed back unchanged for another doomed retry.
        """
        fd = await _acquire_lock_async(self.store.path)
        try:
            tokens = self.store.reload()
            if tokens is None:
                raise TeslaAuthError("Not logged in.")
            if not tokens.expired and (stale is None or tokens.access_token != stale):
                return tokens          # another process already did the work
            return await self._refresh(tokens)
        finally:
            _release_lock(fd)

    async def _access_token(self) -> str:
        tokens = self.store.load()
        if tokens is None:
            raise TeslaAuthError("Not logged in.")
        if not tokens.expired:
            return tokens.access_token
        async with self._refresh_lock:            # coroutines in THIS process
            return (await self._locked_refresh()).access_token

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
                # Access token rejected despite not looking expired — force one
                # refresh, retry once.
                async with self._refresh_lock:
                    await self._locked_refresh(stale=token)
                continue
            if resp.status_code != 200:
                raise TeslaAPIError(resp.status_code, resp.text)
            payload = resp.json().get("response")
            if ttl:
                self.cache.set(key, payload, ttl)
            return payload
        raise TeslaAuthError("Could not authenticate to Fleet API.")

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self.settings.api_base}{path}"
        for attempt in range(2):
            token = await self._access_token()
            resp = await self._http.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 401 and attempt == 0:
                # Access token rejected despite not looking expired — force one
                # refresh, retry once.
                async with self._refresh_lock:
                    await self._locked_refresh(stale=token)
                continue
            if resp.status_code != 200:
                raise TeslaAPIError(resp.status_code, resp.text)
            return resp.json().get("response")
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

    # ---------- vehicles ----------

    async def vehicles(self) -> list[dict[str, Any]]:
        return await self._get("/api/1/vehicles", ttl=60) or []

    async def resolve_vin(self) -> str:
        """Configured VIN wins; otherwise the single vehicle on the account."""
        if self.settings.vin:
            return self.settings.vin
        cars = await self.vehicles()
        if not cars:
            raise TeslaAPIError(404, "No vehicles on this Tesla account.")
        return str(cars[0]["vin"])

    async def vehicle(self, vin: str) -> dict[str, Any]:
        """Cheap state check: online | asleep | offline. Gate paid calls on this."""
        return await self._get(f"/api/1/vehicles/{vin}", ttl=10)

    async def fleet_status(self, vins: list[str]) -> dict[str, Any]:
        return await self._post("/api/1/vehicles/fleet_status", {"vins": vins})

    async def vehicle_data(
        self, vin: str, endpoints: list[str] | None = None
    ) -> dict[str, Any]:
        try:
            return await self._get(
                f"/api/1/vehicles/{vin}/vehicle_data",
                params={"endpoints": endpoints_param(endpoints or VEHICLE_ENDPOINTS)},
                ttl=10,
            )
        except TeslaAPIError as exc:
            if exc.status == 408:
                raise VehicleAsleep(vin) from exc
            raise

    async def wake_up(self, vin: str) -> dict[str, Any]:
        """Returns a vehicle object with `state`, NOT a {result, reason} envelope.
        Expensive and rate-limited to 3/min. Only ever call this on explicit
        user action."""
        return await self._post(f"/api/1/vehicles/{vin}/wake_up", {})

    # ---------- signed commands ----------

    async def command(
        self, vin: str, name: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Signed commands via the local proxy.

        Only /command/* goes through the proxy — reads stay direct so a stopped
        proxy costs us buttons, not data. The proxy's certificate is self-signed,
        so it is supplied as this client's CA bundle; verification stays ON."""
        if self._proxy is None:
            self._proxy = httpx.AsyncClient(
                verify=str(self.settings.proxy_cert), timeout=30.0
            )
        token = await self._access_token()
        url = f"{self.settings.proxy_url}/api/1/vehicles/{vin}/command/{name}"
        resp = await self._proxy.post(
            url, json=body,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"error": resp.text[:300]}


def proxy_up(proxy_url: str) -> bool:
    """TCP reachability of the signing proxy. BLOCKING -- callers on an event
    loop must use asyncio.to_thread. Commands are impossible without the proxy,
    so this gates anything that would write to the car."""
    parsed = urlparse(proxy_url)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 443), timeout=0.5
        ):
            return True
    except OSError:
        return False


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
