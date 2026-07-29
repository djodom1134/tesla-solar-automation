#!/usr/bin/env bash
# Ship this project to another Mac, running natively under launchd.
#
# WHY NOT DOCKER HERE. Docker Desktop on macOS runs a Linux VM: it needs the
# GUI app running and a logged-in session, and costs a couple of gigabytes of
# RAM to containerise what is three Python processes and a Go binary. The
# launchd arrangement below is the one already controlling this car -- proven
# rather than merely plausible -- and it starts without anyone logging in
# (subject to the auto-login caveat this script checks and reports).
#
# Four transfers, deliberately separate:
#   1. code     -- rsync, excluding everything secret
#   2. binary   -- the signing proxy, copied not compiled when the target
#                  shares this Mac's architecture
#   3. secrets  -- pushed by name, 0600
#   4. database -- SQLite's backup API, never cp (WAL; see below)
#
# It does NOT cut over. Two collectors on one car both issue
# set_charging_amps and both integrate against a meter the other is moving,
# and the proxy's per-VIN mutex cannot help across two machines.
set -euo pipefail

TARGET="${TARGET:-macmini}"                       # ssh alias or user@host
DEST="${DEST:-}"                                  # resolved remotely if empty
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes}"
SSH="ssh $SSH_OPTS"
LABEL_PREFIX="com.tenxcious.tesla"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ 0. probe
say "0/6  probing $TARGET"
read -r R_HOME R_ARCH R_OS <<<"$($SSH "$TARGET" 'echo "$HOME $(uname -m) $(sw_vers -productVersion)"')" \
  || fail "cannot reach $TARGET over ssh -- add your key with:
    ssh-copy-id -i ~/.ssh/id_ed25519 $TARGET"
DEST="${DEST:-$R_HOME/tesla_automation}"
echo "  home=$R_HOME arch=$R_ARCH macOS=$R_OS"
echo "  dest=$DEST"

# The proxy is a compiled binary. Copying it only works same-arch; otherwise
# the target needs Go, and saying so beats shipping something that will not
# execute.
LOCAL_ARCH="$(uname -m)"
PROXY_BIN="$HOME/go/bin/tesla-http-proxy"
[ -x "$PROXY_BIN" ] || fail "no proxy binary at $PROXY_BIN"
COPY_PROXY=1
[ "$R_ARCH" = "$LOCAL_ARCH" ] || COPY_PROXY=0
[ "$COPY_PROXY" = 1 ] && echo "  proxy: copy ($LOCAL_ARCH matches)" \
                      || echo "  proxy: MUST BUILD on target ($R_ARCH != $LOCAL_ARCH)"

# Python floor: the code uses zoneinfo and modern typing, and the pinned
# fastapi/uvicorn wheels expect a current interpreter. Detect rather than
# assume -- macOS still ships an ancient system python3.
R_PY="$($SSH "$TARGET" 'for p in python3.13 python3.12 python3.11 python3; do
    command -v $p >/dev/null 2>&1 || continue
    v=$($p -c "import sys;print(\"%d.%d\"%sys.version_info[:2])" 2>/dev/null) || continue
    case "$v" in 3.1[1-9]|3.[2-9]*) echo "$(command -v $p) $v"; break;; esac
  done')"
[ -n "$R_PY" ] || fail "no python >= 3.11 on $TARGET. Install one first:
    brew install python@3.13"
echo "  python: $R_PY"
R_PY_BIN="${R_PY%% *}"

# ------------------------------------------------------------------- 1. code
say "1/6  code -> $TARGET:$DEST"
$SSH "$TARGET" "mkdir -p '$DEST' '$DEST/keys' && chmod 700 '$DEST/keys'"
rsync -az --delete \
  --exclude='.git/' --exclude='.venv/' --exclude='__pycache__/' \
  --exclude='.pytest_cache/' --exclude='.superpowers/' \
  --exclude='.env' --exclude='.env.*' \
  --exclude='.tokens.json*' --exclude='.tokens.lock' --exclude='keys/' \
  --exclude='car.db*' --exclude='data/' --exclude='*.log' \
  -e "$SSH" ./ "$TARGET:$DEST/"

# ----------------------------------------------------------------- 2. binary
say "2/6  signing proxy"
if [ "$COPY_PROXY" = 1 ]; then
  $SSH "$TARGET" "mkdir -p '$R_HOME/go/bin'"
  rsync -az -e "$SSH" "$PROXY_BIN" "$TARGET:$R_HOME/go/bin/tesla-http-proxy"
  $SSH "$TARGET" "chmod 755 '$R_HOME/go/bin/tesla-http-proxy'"
  # Gatekeeper quarantines binaries arriving over the network on some setups;
  # a quarantined proxy dies instantly and launchd respawns it forever.
  $SSH "$TARGET" "xattr -d com.apple.quarantine '$R_HOME/go/bin/tesla-http-proxy' 2>/dev/null; true"
  echo "  copied"
else
  $SSH "$TARGET" "command -v go >/dev/null" \
    || fail "target is $R_ARCH and has no Go. Install it, or build the binary there."
  $SSH "$TARGET" "go install github.com/teslamotors/vehicle-command/cmd/tesla-http-proxy@latest" \
    && echo "  built on target"
fi

# ---------------------------------------------------------------- 3. secrets
say "3/6  secrets (0600)"
for f in .env .tokens.json keys/private-key.pem keys/tls-cert.pem keys/tls-key.pem; do
  [ -f "$f" ] || fail "missing $f -- refusing to deploy a half-configured app"
done
scp $SSH_OPTS -q .env "$TARGET:$DEST/.env"
scp $SSH_OPTS -q .tokens.json "$TARGET:$DEST/.tokens.json"
scp $SSH_OPTS -q keys/private-key.pem keys/public-key.pem \
                 keys/tls-cert.pem keys/tls-key.pem "$TARGET:$DEST/keys/"
$SSH "$TARGET" "chmod 600 '$DEST/.env' '$DEST/.tokens.json' '$DEST'/keys/*-key.pem"

# --------------------------------------------------------------- 4. database
say "4/6  database snapshot"
# NOT cp. car.db runs in WAL mode with a multi-megabyte -wal sidecar; copying
# the main file alone yields a database missing its most recent transactions,
# which here means missing charge history and a stale solar ledger. The backup
# API takes a consistent snapshot of a database being actively written to.
python3 - <<'PY'
import sqlite3, pathlib
src = sqlite3.connect("file:car.db?mode=ro", uri=True)
out = pathlib.Path("/tmp/car.db.snapshot"); out.unlink(missing_ok=True)
dst = sqlite3.connect(out); src.backup(dst); dst.close(); src.close()
print(f"  snapshot {out.stat().st_size:,} bytes")
PY
scp $SSH_OPTS -q /tmp/car.db.snapshot "$TARGET:$DEST/car.db"
rm -f /tmp/car.db.snapshot

# ------------------------------------------------------------------- 5. venv
say "5/6  venv + dependencies"
$SSH "$TARGET" "cd '$DEST' && '$R_PY_BIN' -m venv .venv \
  && ./.venv/bin/pip -q install --upgrade pip \
  && ./.venv/bin/pip -q install -r requirements.txt \
  && ./.venv/bin/python -c 'import fastapi,uvicorn,httpx,dotenv;print(\"  deps ok\")'"

say "  running the suite on the target"
$SSH "$TARGET" "cd '$DEST' && DEMO=1 ./.venv/bin/python -m pytest -q -p no:randomly 2>&1 | tail -3"

# ---------------------------------------------------------------- 6. launchd
say "6/6  launchd agents"
# Generated remotely with the TARGET's paths. The app gets an agent too --
# there was none on the source Mac, which is why the dashboard did not come
# back after a restart and had to be started by hand.
$SSH "$TARGET" "bash -s" <<EOF
set -e
mkdir -p "\$HOME/Library/LaunchAgents"
mk() {  # label, program-args-xml
  cat > "\$HOME/Library/LaunchAgents/\$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>\$1</string>
  <key>ProgramArguments</key><array>\$2</array>
  <key>WorkingDirectory</key><string>$DEST</string>
  <key>StandardOutPath</key><string>$DEST/\$3.log</string>
  <key>StandardErrorPath</key><string>$DEST/\$3.log</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>\$4</integer>
</dict></plist>
PLIST
  plutil -lint "\$HOME/Library/LaunchAgents/\$1.plist" >/dev/null
  echo "  wrote \$1"
}

mk "$LABEL_PREFIX-proxy" "\
<string>\$HOME/go/bin/tesla-http-proxy</string>\
<string>-tls-key</string><string>$DEST/keys/tls-key.pem</string>\
<string>-cert</string><string>$DEST/keys/tls-cert.pem</string>\
<string>-key-file</string><string>$DEST/keys/private-key.pem</string>\
<string>-host</string><string>localhost</string>\
<string>-port</string><string>4443</string>" proxy 30

mk "$LABEL_PREFIX-collector" "\
<string>$DEST/.venv/bin/python</string><string>$DEST/collector.py</string>" collector 60

mk "$LABEL_PREFIX-app" "\
<string>$DEST/.venv/bin/python</string><string>$DEST/app.py</string>" app 30
EOF

# Whether any of this survives a reboot unattended depends on auto-login:
# LaunchAgents start with a user session, not at boot.
say "reboot behaviour"
$SSH "$TARGET" 'AL=$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null || true)
if [ -n "$AL" ]; then echo "  auto-login as \"$AL\" -- agents WILL start after a reboot"
else echo "  NO auto-login: LaunchAgents start with a user session, so after a"
     echo "  reboot nothing runs until someone logs in. Either enable auto-login,"
     echo "  or move these to /Library/LaunchDaemons (root, starts at boot)."; fi'

cat <<EOF

Staged, not started. Cutover is one-machine-at-a-time:

  1. stop this Mac's controller, so two do not fight over the car:
       launchctl bootout gui/\$(id -u)/$LABEL_PREFIX-collector
       launchctl bootout gui/\$(id -u)/$LABEL_PREFIX-proxy

  2. start the target's:
       $SSH $TARGET 'launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/$LABEL_PREFIX-proxy.plist
                     launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/$LABEL_PREFIX-collector.plist
                     launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/$LABEL_PREFIX-app.plist'

  3. watch it take over:
       $SSH $TARGET 'tail -f $DEST/collector.log'

This Mac's database and agents are untouched, so rolling back is step 1 in
reverse.
EOF
