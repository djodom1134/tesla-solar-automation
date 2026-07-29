#!/usr/bin/env bash
# Ship this project to a Docker host.
#
# Three transfers, deliberately separate:
#   1. code      -- rsync, excludes everything secret (see EXCLUDES)
#   2. secrets   -- pushed by name, 0600, never through git and never rsynced
#                   as a directory sweep that could pick up a stray file
#   3. database  -- copied via SQLite's own backup API, not cp
#
# THE DATABASE IS COPIED LIVE AND THAT IS WHY IT USES .backup. car.db runs in
# WAL mode with a multi-megabyte -wal sidecar; `cp car.db` grabs the main file
# without the WAL and yields a database missing its most recent transactions,
# which here means missing charge history and a stale ledger. The backup API
# takes a consistent snapshot of a database being written to.
#
# It does NOT cut over. Starting the remote collector while the local one still
# runs puts two controllers on one car: both issue set_charging_amps, both
# integrate against a meter the other is moving, and the per-VIN mutex inside
# the signing proxy cannot help because they are different proxies on different
# machines. Cutover is a separate, deliberate step -- see the closing message.
set -euo pipefail

HOST="${HOST:-192.168.87.6}"
USER="${USER_REMOTE:-d}"
KEY="${KEY:-$HOME/.ssh/servy_ed25519}"
DEST="${DEST:-/home/d/tesla_automation}"
SSH="ssh -i $KEY -o BatchMode=yes"

SECRETS=(.env .tokens.json)
KEYFILES=(keys/private-key.pem keys/public-key.pem keys/tls-cert.pem keys/tls-key.pem)

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/4  code -> $USER@$HOST:$DEST"
# --delete keeps the remote tree honest, but the excludes are load-bearing:
# without them it would delete the remote data/ and keys/ on the first run.
rsync -az --delete \
  --exclude='.git/' --exclude='.venv/' --exclude='__pycache__/' \
  --exclude='.pytest_cache/' --exclude='.superpowers/' \
  --exclude='.env' --exclude='.env.*' \
  --exclude='.tokens.json*' --exclude='keys/' \
  --exclude='car.db*' --exclude='data/' --exclude='*.log' \
  -e "$SSH" ./ "$USER@$HOST:$DEST/"

say "2/4  secrets (0600) and keys (0700 dir)"
$SSH "$USER@$HOST" "mkdir -p '$DEST/keys' '$DEST/data' '$DEST/secrets' \
  && chmod 700 '$DEST/keys' '$DEST/secrets'"
for f in "${SECRETS[@]}"; do
  [ -f "$f" ] || { echo "  missing $f -- refusing to deploy a half-configured app"; exit 1; }
done
# .env lands as secrets/app.env, deliberately NOT the project's own .env:
# Compose auto-reads ./.env to interpolate its own file, so leaving real
# secrets there feeds them to an interpolator that has no need for them and
# mangles any value containing a `$`.
#
# .tokens.json goes to the data volume because the app REWRITES it on every
# token refresh; a read-only mount would break re-authentication some hours
# after a clean-looking deploy.
scp -i "$KEY" -q .env "$USER@$HOST:$DEST/secrets/app.env"
scp -i "$KEY" -q .tokens.json "$USER@$HOST:$DEST/data/.tokens.json"
scp -i "$KEY" -q "${KEYFILES[@]}" "$USER@$HOST:$DEST/keys/"
$SSH "$USER@$HOST" "chmod 600 '$DEST/secrets/app.env' '$DEST/data/.tokens.json' '$DEST'/keys/*.pem"

say "3/4  database snapshot (SQLite backup API, safe on a live file)"
python3 - "$DEST" <<'PY'
import sqlite3, sys, pathlib
src = sqlite3.connect("file:car.db?mode=ro", uri=True)
out = pathlib.Path("/tmp/car.db.snapshot")
out.unlink(missing_ok=True)
dst = sqlite3.connect(out)
src.backup(dst)          # consistent even while the collector is writing
dst.close(); src.close()
print(f"  snapshot {out.stat().st_size:,} bytes")
PY
scp -i "$KEY" -q /tmp/car.db.snapshot "$USER@$HOST:$DEST/data/car.db"
rm -f /tmp/car.db.snapshot

say "4/4  build"
$SSH "$USER@$HOST" "cd '$DEST' && docker compose build"

cat <<EOF

Built, not started. Cutover is manual and one-way-at-a-time:

  1. stop the local controller so two do not fight over the car:
       launchctl bootout gui/\$(id -u)/com.tenxcious.tesla-collector
       launchctl bootout gui/\$(id -u)/com.tenxcious.tesla-proxy

  2. start the remote one:
       $SSH $USER@$HOST 'cd $DEST && docker compose up -d'

  3. watch it take over:
       $SSH $USER@$HOST 'cd $DEST && docker compose logs -f collector'

To roll back, stop the containers and re-enable the launchd agents. The
database on this Mac is untouched by this script.
EOF
