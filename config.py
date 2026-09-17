"""Configuration loaded from the environment (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# Fleet API base URLs. The token `audience` must equal the base URL it will be used against.
REGIONS = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    "cn": "https://fleet-api.prd.cn.vn.cloud.tesla.cn",
}

AUTH_BASE = "https://auth.tesla.com"
# Token exchange must go to fleet-auth, not auth.tesla.com — different rate limits.
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"

# Read-only energy access plus vehicle reads and commands.
# openid+offline_access are what get us a refresh token.
# `vehicle_location` is the scope; `location_data` is a value of the
# vehicle_data `endpoints` param — both are required for coordinates.
SCOPES = [
    "openid",
    "offline_access",
    "energy_device_data",
    "vehicle_device_data",
    "vehicle_location",
    "vehicle_cmds",
    "vehicle_charging_cmds",
]


def _clean(value: str | None) -> str:
    """Strip whitespace and stray quotes people leave in .env files."""
    return (value or "").strip().strip("'\"")


@dataclass
class Settings:
    client_id: str = field(default_factory=lambda: _clean(os.getenv("TESLA_CLIENT_ID")))
    client_secret: str = field(default_factory=lambda: _clean(os.getenv("TESLA_CLIENT_SECRET")))
    redirect_uri: str = field(
        default_factory=lambda: _clean(os.getenv("TESLA_REDIRECT_URI"))
        or "http://localhost:8000/auth/callback"
    )
    region: str = field(default_factory=lambda: _clean(os.getenv("TESLA_REGION")).lower() or "na")
    domain: str = field(default_factory=lambda: _clean(os.getenv("TESLA_DOMAIN")))
    timezone: str = field(
        default_factory=lambda: _clean(os.getenv("TESLA_TIMEZONE")) or "America/Los_Angeles"
    )
    host: str = field(default_factory=lambda: _clean(os.getenv("HOST")) or "127.0.0.1")
    port: int = field(default_factory=lambda: int(_clean(os.getenv("PORT")) or 8000))
    token_file: Path = field(
        default_factory=lambda: Path(_clean(os.getenv("TESLA_TOKEN_FILE")) or BASE_DIR / ".tokens.json")
    )
    vin: str = field(default_factory=lambda: _clean(os.getenv("TESLA_VIN")))
    db_file: Path = field(
        default_factory=lambda: Path(_clean(os.getenv("CAR_DB")) or BASE_DIR / "car.db")
    )
    # Signed commands only. Reads go direct so proxy downtime costs us buttons, not data.
    proxy_url: str = field(
        default_factory=lambda: _clean(os.getenv("TESLA_PROXY_URL")) or "https://localhost:4443"
    )
    proxy_cert: Path = field(
        default_factory=lambda: Path(
            _clean(os.getenv("TESLA_PROXY_CERT")) or BASE_DIR / "keys" / "tls-cert.pem"
        )
    )
    # Adaptive poll intervals, seconds. Asleep uses the cheap, sleep-safe state
    # check only. NOT free: Tesla bills every response with status < 500.
    poll_driving: int = field(default_factory=lambda: int(_clean(os.getenv("POLL_DRIVING")) or 120))
    poll_charging: int = field(default_factory=lambda: int(_clean(os.getenv("POLL_CHARGING")) or 300))
    poll_idle: int = field(default_factory=lambda: int(_clean(os.getenv("POLL_IDLE")) or 1800))
    poll_asleep: int = field(default_factory=lambda: int(_clean(os.getenv("POLL_ASLEEP")) or 1800))

    # Optional: price per kWh, purely for the cost/credit readout. Blank disables it.
    import_rate: float | None = field(
        default_factory=lambda: float(_clean(os.getenv("IMPORT_RATE_PER_KWH")) or 0) or None
    )
    export_rate: float | None = field(
        default_factory=lambda: float(_clean(os.getenv("EXPORT_RATE_PER_KWH")) or 0) or None
    )
    currency: str = field(default_factory=lambda: _clean(os.getenv("CURRENCY")) or "USD")

    # Three separate secrets so each consumer rotates independently: the
    # browser UI, Home Assistant, and the MCP endpoint. A leaked HA token
    # must not also be an MCP credential.
    api_token: str = field(default_factory=lambda: _clean(os.getenv("API_TOKEN")))
    api_token_ha: str = field(default_factory=lambda: _clean(os.getenv("API_TOKEN_HA")))
    api_token_mcp: str = field(default_factory=lambda: _clean(os.getenv("API_TOKEN_MCP")))
    # Extra Host header values /mcp will answer to, comma-separated, each
    # WITH its port (the Host header carries one). The MCP SDK's DNS-rebinding
    # protection allows 127.0.0.1 only by default, which 421s every LAN
    # client -- and reaching this box from another machine is the entire
    # point. Named explicitly rather than wildcarded: the guard stays on.
    mcp_allowed_hosts: str = field(
        default_factory=lambda: _clean(os.getenv("MCP_ALLOWED_HOSTS")))

    @property
    def api_base(self) -> str:
        if self.region not in REGIONS:
            raise ValueError(f"TESLA_REGION must be one of {sorted(REGIONS)}, got {self.region!r}")
        return REGIONS[self.region]

    @property
    def audience(self) -> str:
        return self.api_base

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


settings = Settings()
