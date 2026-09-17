# Setup

Fleet API is not a "paste an API key" affair — Tesla makes every caller register as a
partner and prove they control a domain, even a hobbyist reading their own Powerwall.
That's the bulk of the work below. Budget ~20 minutes; steps 1–3 are one-time and
you'll never touch them again.

Steps 1 and 2 are yours to do — they involve creating an account and agreeing to
Tesla's terms, which nobody should do on your behalf.

---

## 1. Tesla account

Use the Tesla account that **owns the solar system**. It needs a verified email and
multi-factor authentication turned on — Fleet API rejects accounts without MFA.

## 2. Create the app

Go to **https://developer.tesla.com** → create an application.

| Field | Value |
|---|---|
| Scopes | **Energy Product Information** (`energy_device_data`). That's all this dashboard needs — it never writes. |
| Allowed origin | A domain you control, e.g. `example.com` |
| Redirect URI | `http://localhost:8000/auth/callback` |

Two things to know:

- **App names are globally unique.** A name someone else already took gets
  auto-rejected, so pick something distinctive.
- **The redirect URI must match byte-for-byte** what goes in `.env`. If Tesla won't
  accept a `localhost` URI on your account, register one on your own domain instead
  (e.g. `https://example.com/callback`, pointing anywhere — even a 404 is fine, you
  only need the `?code=` in the address bar) and use the **Paste callback URL** flow
  the dashboard offers.

Copy the **client ID** and **client secret** at the end. The secret is shown once.

## 3. Configure and register

```bash
cp .env.example .env          # then fill in CLIENT_ID, CLIENT_SECRET, DOMAIN, TIMEZONE
pip install -r requirements.txt

python setup_tesla.py keys    # generates keys/private-key.pem + keys/public-key.pem
```

Now host `keys/public-key.pem` so that this **exact** URL serves it over HTTPS:

```
https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem
```

Any static host works — GitHub Pages, Cloudflare Pages, S3, an nginx you already run.
The file is public by design; the *private* key never leaves your machine and is
gitignored.

> Tesla fetches this URL itself, so it has to be reachable from the public internet.
> A local server or a Cloudflare "under attack" mode will fail the check.

Then register:

```bash
python setup_tesla.py register
```

This mints a partner token, calls `POST /api/1/partner_accounts` with your domain, and
verifies Tesla can read the key back. Registration is **per region** — if you later
move regions, run it again with the new `TESLA_REGION`.

## 4. Run

```bash
python app.py           # -> http://localhost:8000
```

Click **Connect Tesla account**, approve the scopes, and you're in.

---

## If something breaks

Run the doctor first — it checks `.env`, the key pair, whether your public key is
actually live and matches, and whether you're logged in:

```bash
python setup_tesla.py doctor
```

| Symptom | Cause |
|---|---|
| `register failed (412)` | Tesla couldn't fetch your public key. Open the `.well-known` URL in a private window — it must return the PEM over HTTPS with no redirect to a login page. |
| `invalid_auth_code` | The authorization code expired (they're short-lived). Just start the login again. |
| `login_required` on refresh | The refresh token expired (they last 3 months), was superseded, or the Tesla account password changed. Click Disconnect, then Connect again. |
| Charts are empty but auth works | Check `TESLA_TIMEZONE` is the **site's** timezone. Tesla buckets energy by local day; a wrong zone can put "today" in a window with no data yet. |
| `403` on every call | Registration didn't complete in this region. Re-run `python setup_tesla.py register`. |

## Costs

Fleet API is metered. Authentication calls aren't billed, and Tesla has historically
included a free monthly allowance that a single-household dashboard sits well inside —
this app also caches (live status 15s, history 2min) and pulls one combined request per
view rather than one per tile. Check current pricing on developer.tesla.com; if you
leave it running 24/7, widen the refresh interval in `static/app.js` (`30_000`).
