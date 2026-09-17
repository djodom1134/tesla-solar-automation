"""One-time Fleet API onboarding: key pair -> partner token -> register domain -> verify.

    python setup_tesla.py keys      # generate the EC key pair to host on your domain
    python setup_tesla.py register  # partner token + POST /api/1/partner_accounts
    python setup_tesla.py verify    # confirm Tesla can read your hosted public key
    python setup_tesla.py doctor    # check .env, key, registration, and login end to end
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx

from config import BASE_DIR, TOKEN_URL, settings

KEY_DIR = BASE_DIR / "keys"
PRIVATE_KEY = KEY_DIR / "private-key.pem"
PUBLIC_KEY = KEY_DIR / "public-key.pem"
WELL_KNOWN = ".well-known/appspecific/com.tesla.3p.public-key.pem"

OK, BAD, WARN = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[33m!\033[0m"


def cmd_keys() -> None:
    """Generate the prime256v1 key pair Tesla uses to prove you control the domain."""
    KEY_DIR.mkdir(exist_ok=True)
    if PRIVATE_KEY.exists():
        print(f"{WARN} {PRIVATE_KEY} already exists — leaving it alone.")
    else:
        subprocess.run(
            ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(PRIVATE_KEY)],
            check=True,
        )
        PRIVATE_KEY.chmod(0o600)
        print(f"{OK} wrote {PRIVATE_KEY}  (secret — never host or commit this)")

    subprocess.run(
        ["openssl", "ec", "-in", str(PRIVATE_KEY), "-pubout", "-out", str(PUBLIC_KEY)],
        check=True,
        capture_output=True,
    )
    print(f"{OK} wrote {PUBLIC_KEY}")
    domain = settings.domain or "<your-domain>"
    print(
        f"\nHost the PUBLIC key so this exact URL serves it:\n"
        f"  https://{domain}/{WELL_KNOWN}\n\n"
        f"It must be served over HTTPS as text, and the domain must match the root domain\n"
        f"of the Allowed Origin you set on developer.tesla.com.\n"
        f"Then run:  python setup_tesla.py register\n"
    )


def _partner_token() -> str:
    """A partner token (client_credentials) — distinct from the user token the app runs on."""
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "scope": "openid energy_device_data",
            "audience": settings.audience,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        sys.exit(f"{BAD} partner token failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["access_token"]


def _local_point_hex() -> str | None:
    """The uncompressed EC point (04‖X‖Y) hex from our public key, to compare against
    what Tesla echoes back. For a prime256v1 SPKI the point is the trailing 65 bytes."""
    try:
        import base64

        body = "".join(ln for ln in PUBLIC_KEY.read_text().splitlines() if "-----" not in ln)
        return base64.b64decode(body)[-65:].hex()
    except Exception:
        return None


def cmd_register() -> None:
    if not settings.domain:
        sys.exit(f"{BAD} set TESLA_DOMAIN in .env first (the domain hosting your public key).")
    token = _partner_token()
    print(f"{OK} got partner token")

    resp = httpx.post(
        f"{settings.api_base}/api/1/partner_accounts",
        headers={"Authorization": f"Bearer {token}"},
        json={"domain": settings.domain},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        sys.exit(
            f"{BAD} register failed ({resp.status_code}): {resp.text[:400]}\n\n"
            f"The usual cause is that Tesla could not fetch\n"
            f"  https://{settings.domain}/{WELL_KNOWN}\n"
            f"Check it loads publicly over HTTPS, then retry."
        )
    print(f"{OK} registered {settings.domain} in region '{settings.region}'")

    # Confirm straight from the POST response — it echoes the stored account, including the
    # public key. (The dedicated verify endpoint needs a scope an energy-only app isn't granted.)
    account = (resp.json() or {}).get("response") or {}
    remote = (account.get("public_key") or "").lower()
    local = (_local_point_hex() or "").lower()
    if remote and local and remote == local:
        print(f"{OK} Tesla stored the public key matching keys/public-key.pem")
    elif remote and local:
        print(f"{WARN} Tesla stored a public key that does NOT match keys/public-key.pem — "
              f"re-upload the current key to your domain and re-register.")
    cmd_verify()
    print("\nNow start the app and log in:\n  python app.py\n")


def cmd_verify() -> None:
    token = _partner_token()
    resp = httpx.get(
        f"{settings.api_base}/api/1/partner_accounts/public_key",
        headers={"Authorization": f"Bearer {token}"},
        params={"domain": settings.domain},
        timeout=30,
    )
    if resp.status_code == 200:
        print(f"{OK} Tesla has your public key on file: {resp.json()}")
        return
    # Non-fatal: an app scoped to energy_device_data can't read this endpoint. Registration
    # is already confirmed from the register response above, so this is informational only.
    err = ""
    try:
        err = resp.json().get("error", "")
    except Exception:
        pass
    print(f"{WARN} verify read returned {resp.status_code} ({err}); this endpoint needs a scope "
          f"an energy-only app isn't granted — registration is unaffected.")


def cmd_doctor() -> None:
    print("Checking setup...\n")
    ok = True

    for name, value in (
        ("TESLA_CLIENT_ID", settings.client_id),
        ("TESLA_CLIENT_SECRET", settings.client_secret),
        ("TESLA_DOMAIN", settings.domain),
    ):
        if value:
            shown = value if name != "TESLA_CLIENT_SECRET" else "•" * 12
            print(f"  {OK} {name} = {shown}")
        else:
            print(f"  {BAD} {name} is not set in .env")
            ok = False

    print(f"  {OK} region '{settings.region}' -> {settings.api_base}")
    print(f"  {OK} redirect_uri = {settings.redirect_uri}")
    print(f"  {OK} timezone = {settings.timezone}")

    if PUBLIC_KEY.exists():
        print(f"  {OK} local key pair present")
    else:
        print(f"  {BAD} no key pair — run: python setup_tesla.py keys")
        ok = False

    if settings.domain:
        url = f"https://{settings.domain}/{WELL_KNOWN}"
        try:
            resp = httpx.get(url, timeout=15, follow_redirects=True)
            if resp.status_code == 200 and "PUBLIC KEY" in resp.text:
                local = PUBLIC_KEY.read_text().strip() if PUBLIC_KEY.exists() else ""
                if local and local in resp.text.strip():
                    print(f"  {OK} public key is live and matches your local copy")
                else:
                    print(f"  {WARN} {url} serves a key, but it is NOT the one in keys/public-key.pem")
                    ok = False
            else:
                print(f"  {BAD} {url} -> HTTP {resp.status_code} (Tesla must be able to fetch this)")
                ok = False
        except httpx.HTTPError as exc:
            print(f"  {BAD} could not reach {url}: {exc}")
            ok = False

    tokens = Path(settings.token_file)
    if tokens.exists():
        print(f"  {OK} logged in (tokens at {tokens.name})")
    else:
        print(f"  {WARN} not logged in yet — run `python app.py` and click Connect")

    print("\n" + ("All good." if ok else "Fix the ✗ items above, then re-run `doctor`."))


COMMANDS = {"keys": cmd_keys, "register": cmd_register, "verify": cmd_verify, "doctor": cmd_doctor}

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "doctor"
    if action not in COMMANDS:
        sys.exit(f"usage: python setup_tesla.py [{' | '.join(COMMANDS)}]")
    # `keys` and `doctor` must work before the app is configured — doctor's whole job
    # is to diagnose a partial setup, and keygen has no dependency on the credentials.
    if action not in ("keys", "doctor") and not settings.configured:
        sys.exit(f"{BAD} TESLA_CLIENT_ID / TESLA_CLIENT_SECRET missing from .env — see SETUP.md")
    COMMANDS[action]()
