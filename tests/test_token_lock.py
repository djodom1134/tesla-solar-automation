"""Two processes must never both spend the same single-use refresh token."""
from __future__ import annotations

import fcntl
import json
import multiprocessing as mp
import os
import threading
import time
from pathlib import Path

import httpx
import pytest

from config import Settings, TOKEN_URL
from tesla import TeslaClient, TokenStore, _acquire_lock, _release_lock


def _write(path: Path, access: str, expires_at: float) -> None:
    path.write_text(json.dumps(
        {"access_token": access, "refresh_token": "r0", "expires_at": expires_at}))


def test_reload_sees_another_process_write(tmp_path):
    """load() caches; reload() must not."""
    p = tmp_path / ".tokens.json"
    _write(p, "first", time.time() + 9999)
    store = TokenStore(p)
    assert store.load().access_token == "first"

    _write(p, "second", time.time() + 9999)     # simulates the other process
    assert store.load().access_token == "first"    # cached, as designed
    assert store.reload().access_token == "second"  # forced re-read


def _hold(path_str: str, started, release):
    """Child: take the lock, signal, hold until told to let go."""
    fd = _acquire_lock(Path(path_str))
    started.set()
    release.wait(timeout=10)
    _release_lock(fd)


def test_lock_is_exclusive_across_processes(tmp_path):
    p = tmp_path / ".tokens.json"
    _write(p, "first", time.time() + 9999)
    started, release = mp.Event(), mp.Event()
    child = mp.Process(target=_hold, args=(str(p), started, release))
    child.start()
    try:
        assert started.wait(timeout=10), "child never acquired"

        # (a) exclusion: a non-blocking take must FAIL while the child holds it.
        probe = os.open(tmp_path / ".tokens.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)

        # (b) blocking: release only after a measurable delay.
        threading.Timer(0.5, release.set).start()
        t0 = time.monotonic()
        fd = _acquire_lock(p)          # must block until the child releases
        waited = time.monotonic() - t0
        _release_lock(fd)
        assert waited >= 0.4, f"acquire did not block ({waited:.3f}s)"
    finally:
        release.set()
        child.join(timeout=10)


def test_lock_file_is_a_sidecar_not_the_token_file(tmp_path):
    """save() replaces the token file's inode; an flock on it would be lost."""
    p = tmp_path / ".tokens.json"
    _write(p, "first", time.time() + 9999)
    fd = _acquire_lock(p)
    _release_lock(fd)
    assert (tmp_path / ".tokens.lock").exists()


@pytest.mark.asyncio
async def test_401_on_unexpired_token_forces_a_real_refresh(tmp_path):
    """A 401 from the Fleet API fires precisely when the access token does NOT
    look expired -- an expired one was already refreshed by _access_token
    before the request was even sent. Regression for a defect where
    `_locked_refresh` short-circuited with `if not tokens.expired: return
    tokens`, handing the same just-rejected token back unchanged, so the
    retry resent it and got a second 401 instead of a working token."""
    token_path = tmp_path / ".tokens.json"
    _write(token_path, "stale_access", time.time() + 9999)  # NOT expired, but the API will reject it

    settings = Settings(client_id="cid", client_secret="csecret", token_file=token_path)
    client = TeslaClient(settings)
    refresh_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and str(request.url) == TOKEN_URL:
            refresh_calls.append(request)
            return httpx.Response(
                200,
                json={"access_token": "new_access", "refresh_token": "r1", "expires_in": 28800},
            )
        if request.method == "GET":
            if request.headers.get("authorization") == "Bearer new_access":
                return httpx.Response(200, json={"response": {"ok": True}})
            return httpx.Response(401, json={})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    old_http = client._http
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0)
    await old_http.aclose()
    try:
        result = await client._get("/api/1/vehicles/VIN/vehicle_data")
    finally:
        await client.aclose()

    assert result == {"ok": True}
    assert len(refresh_calls) == 1, "the stale-but-unexpired token must trigger exactly one real refresh"
    assert client.store.load().access_token == "new_access"
