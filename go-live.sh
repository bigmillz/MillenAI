#!/bin/bash
# MillenAI go-live — ONE command, run it and walk away:
#
#   ./go-live.sh
#
# Installs a self-updating, always-on MillenAI you can reach from anywhere:
#   * a managed clone of the repo in ~/Library/MillenAI-live, pinned to the
#     newest RELEASE tag (not main — you only ever serve shipped builds)
#   * a LaunchAgent that keeps a headless MillenAI server running on :9889
#     (your desktop app keeps :8889, engines keep 88xx — they coexist)
#   * a LaunchAgent that checks for a new release every hour and
#     restarts the server on the new build automatically
#   * a Cloudflare named tunnel at https://ai.millertechnology.net
#     (the ONE step that needs a human: if the tunnel was never authorized,
#     a browser page opens — click your zone, click Authorize. The script
#     waits for you, and everything else is already installed either way.)
#
# Idempotent: re-running repairs/updates the installation, never duplicates.
set -euo pipefail

REPO="bigmillz/MillenAI"
LIVE="$HOME/Library/MillenAI-live"
HOST="ai.millertechnology.net"
SERVE_PORT=9889
TUNNEL="millenai"
LABEL="net.millertechnology.millenai"
APPVENV="$HOME/Library/Application Support/MillenAI/venv"

say(){ printf '\n\033[1m» %s\033[0m\n' "$*"; }

mkdir -p "$LIVE"

# ------------------------------------------------------------- access key
# Never committed anywhere: lives only in $LIVE/key (0600) and the plist.
if [ -f "$LIVE/key" ]; then
  KEY=$(cat "$LIVE/key")
elif [ -n "$(launchctl getenv MILLENAI_KEY 2>/dev/null || true)" ]; then
  KEY=$(launchctl getenv MILLENAI_KEY)
  printf '%s' "$KEY" > "$LIVE/key" && chmod 600 "$LIVE/key"
else
  KEY=$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 28)
  printf '%s' "$KEY" > "$LIVE/key" && chmod 600 "$LIVE/key"
fi

# ------------------------------------------------- repo, pinned to release
say "syncing the live copy to the newest release"
if [ ! -d "$LIVE/repo/.git" ]; then
  git clone "https://github.com/$REPO" "$LIVE/repo"
fi
cd "$LIVE/repo"
git fetch --tags --force --quiet
TAG=$(git tag -l 'v*' --sort=-v:refname | head -1)
[ -n "$TAG" ] || { echo "no release tags found"; exit 1; }
git checkout --quiet "$TAG"
echo "  serving release $TAG"

# ------------------------------------------------------------------ venv
# The desktop app's venv already holds every engine dependency; reuse it.
# Headless mode needs no pywebview, so a minimal venv also works fresh.
if [ -x "$APPVENV/bin/python3" ]; then
  PY="$APPVENV/bin/python3"
else
  say "building a server venv (first run only)"
  python3 -m venv "$LIVE/venv"
  "$LIVE/venv/bin/pip" -q install --upgrade pip
  "$LIVE/venv/bin/pip" -q install ddgs psutil huggingface_hub mlx_lm
  PY="$LIVE/venv/bin/python3"
fi

# ------------------------------------------------------------- updater
cat > "$LIVE/update.sh" <<EOF
#!/bin/bash
# ran by launchd hourly: move to the newest release tag and restart
set -e
cd "$LIVE/repo"
git fetch --tags --force --quiet
NEW=\$(git tag -l 'v*' --sort=-v:refname | head -1)
CUR=\$(git describe --tags --exact-match 2>/dev/null || echo none)
if [ "\$NEW" != "\$CUR" ]; then
  git checkout --force --quiet "\$NEW"
  launchctl kickstart -k "gui/\$(id -u)/$LABEL" || true
  echo "\$(date '+%F %T') updated \$CUR -> \$NEW" >> "$LIVE/update.log"
fi
EOF
chmod +x "$LIVE/update.sh"

# -------------------------------------------------------- LaunchAgents
say "installing the always-on server + hourly auto-update"
AGENTS="$HOME/Library/LaunchAgents"; mkdir -p "$AGENTS"

cat > "$AGENTS/$LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$LIVE/repo/millenai.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>MILLENAI_HEADLESS</key><string>1</string>
    <key>MILLENAI_PORT</key><string>$SERVE_PORT</string>
    <key>MILLENAI_KEY</key><string>$KEY</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LIVE/server.log</string>
  <key>StandardErrorPath</key><string>$LIVE/server.log</string>
</dict></plist>
EOF

cat > "$AGENTS/$LABEL-update.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL-update</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$LIVE/update.sh</string></array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LIVE/update.log</string>
  <key>StandardErrorPath</key><string>$LIVE/update.log</string>
</dict></plist>
EOF

UID_N=$(id -u)
# bootout is asynchronous — an immediate bootstrap of the same label races
# it and fails with EIO(5). Retry briefly, and fall back to kickstart when
# the agent turns out to be alive already (re-run case).
load_agent(){
  launchctl bootout "gui/$UID_N/$1" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    launchctl bootstrap "gui/$UID_N" "$AGENTS/$1.plist" 2>/dev/null && return 0
    sleep 1
  done
  launchctl kickstart "gui/$UID_N/$1" 2>/dev/null || true
}
for L in "$LABEL" "$LABEL-update"; do
  load_agent "$L"
done

# --------------------------------------------------------------- tunnel
if ! command -v cloudflared >/dev/null; then
  say "cloudflared missing — installing via homebrew"
  brew install cloudflared
fi

if [ ! -f "$HOME/.cloudflared/cert.pem" ]; then
  say "tunnel not authorized yet — your browser is opening Cloudflare now."
  echo "  Click the millertechnology.net row, then Authorize."
  echo "  Waiting up to 5 minutes…"
  cloudflared tunnel login &
  LOGIN_PID=$!
  for _ in $(seq 1 60); do
    [ -f "$HOME/.cloudflared/cert.pem" ] && break
    sleep 5
  done
  kill "$LOGIN_PID" 2>/dev/null || true
fi

if [ -f "$HOME/.cloudflared/cert.pem" ]; then
  if ! cloudflared tunnel list 2>/dev/null | grep -q " $TUNNEL "; then
    cloudflared tunnel create "$TUNNEL"
  fi
  TID=$(cloudflared tunnel list 2>/dev/null | awk -v t="$TUNNEL" '$2==t{print $1}')
  cat > "$HOME/.cloudflared/config.yml" <<EOF
tunnel: $TID
credentials-file: $HOME/.cloudflared/$TID.json
ingress:
  - hostname: $HOST
    service: http://localhost:$SERVE_PORT
  - service: http_status:404
EOF
  cloudflared tunnel route dns "$TUNNEL" "$HOST" 2>/dev/null || true

  cat > "$AGENTS/$LABEL-tunnel.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL-tunnel</string>
  <key>ProgramArguments</key>
  <array><string>$(command -v cloudflared)</string>
  <string>tunnel</string><string>run</string><string>$TUNNEL</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LIVE/tunnel.log</string>
  <key>StandardErrorPath</key><string>$LIVE/tunnel.log</string>
</dict></plist>
EOF
  load_agent "$LABEL-tunnel"

  say "LIVE: https://$HOST/?key=$KEY"
  echo "  (first visit sets a 30-day cookie; after that just https://$HOST)"
else
  say "tunnel still not authorized — everything else IS installed."
  echo "  Local service:  http://localhost:$SERVE_PORT/?key=$KEY"
  echo "  Re-run this script after clicking Authorize to finish the tunnel."
fi
