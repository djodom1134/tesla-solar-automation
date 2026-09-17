"""Invariant 4 (spec 3.7): TeslaAPIError must surface a server-supplied
Retry-After so the collector can honour it, without ever inventing a value
the response did not actually send.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from config import Settings
from tesla import TeslaAPIError, TeslaClient


def _client(tmp_path) -> TeslaClient:
    token_path = tmp_path / ".tokens.json"
    token_path.write_text(json.dumps(
        {"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 9999}))
    settings = Settings(client_id="cid", client_secret="csecret", token_file=token_path)
    return TeslaClient(settings)


@pytest.mark.asyncio
async def test_429_with_an_integer_retry_after_is_captured(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited", headers={"Retry-After": "37"})

    client = _client(tmp_path)
    old_http = client._http
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0)
    await old_http.aclose()
    try:
        with pytest.raises(TeslaAPIError) as excinfo:
            await client._get("/api/1/vehicles/VIN/vehicle_data")
        assert excinfo.value.status == 429
        assert excinfo.value.retry_after == 37.0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_429_with_no_retry_after_header_leaves_it_none(tmp_path):
    """Absent must stay None -- never a guessed value."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    client = _client(tmp_path)
    old_http = client._http
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0)
    await old_http.aclose()
    try:
        with pytest.raises(TeslaAPIError) as excinfo:
            await client._get("/api/1/vehicles/VIN/vehicle_data")
        assert excinfo.value.retry_after is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_429_with_an_http_date_retry_after_falls_back_to_none(tmp_path):
    """The HTTP-date form is a valid Retry-After per RFC 9110, but this code
    does not attempt to parse it -- guessing at a format never measured on
    this account is worse than admitting we don't know."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, text="rate limited",
            headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})

    client = _client(tmp_path)
    old_http = client._http
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0)
    await old_http.aclose()
    try:
        with pytest.raises(TeslaAPIError) as excinfo:
            await client._get("/api/1/vehicles/VIN/vehicle_data")
        assert excinfo.value.retry_after is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_non_429_error_can_still_carry_retry_after(tmp_path):
    """_post shares the same parsing path as _get -- a 503 (proxy/mutex
    contention, spec 5) can carry the same header."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={}, headers={"Retry-After": "5"})

    client = _client(tmp_path)
    old_http = client._http
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0)
    await old_http.aclose()
    try:
        with pytest.raises(TeslaAPIError) as excinfo:
            await client._post("/api/1/vehicles/VIN/wake_up", {})
        assert excinfo.value.status == 503
        assert excinfo.value.retry_after == 5.0
    finally:
        await client.aclose()
