#!/bin/zsh
# Builds MillenAI.app — a double-clickable native macOS bundle.
# Version comes from APP_VERSION/APP_BUILD in millenai.py.
# Creates a private venv so it never fights Homebrew's managed Python (PEP 668).
# Run from the folder containing millenai.py and MillenAI.icns:
#   chmod +x build_macos_app.sh && ./build_macos_app.sh
set -e

BASE_PY="$(command -v python3)"
if [[ -z "$BASE_PY" ]]; then
  echo "python3 not found on PATH"; exit 1
fi

# --- private venv, safely outside Homebrew's jurisdiction
VENV="$HOME/Library/Application Support/MillenAI/venv"
if [[ ! -x "$VENV/bin/python3" ]]; then
  echo "creating venv at: $VENV"
  "$BASE_PY" -m venv "$VENV"
fi

echo "installing dependencies into the venv…"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet pywebview ddgs psutil mlx-lm mlx-whisper
PY="$VENV/bin/python3"
echo "app will run on: $PY"

# Bundle stays version-less on purpose: dropping a new build into
# /Applications then replaces the old one instead of piling up copies.
# The version lives in the UI and on the disk image, not in the filename.
# single source of truth for the version lives in millenai.py
APP_VERSION="$(python3 -c "import re;print(re.search(r'APP_VERSION = \"([^\"]+)\"', open('millenai.py').read()).group(1))")"
APP_BUILD="$(python3 -c "import re;print(re.search(r'APP_BUILD = (\d+)', open('millenai.py').read()).group(1))")"
echo "version: $APP_VERSION (build $APP_BUILD)"

APP="MillenAI.app"
rm -rf "$APP" "MillenAI Beta 2.app"   # drop any old versioned bundle too
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp millenai.py "$APP/Contents/Resources/"
[[ -f MillenAI.icns ]] && cp MillenAI.icns "$APP/Contents/Resources/"

# --- Info.plist
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>            <string>MillenAI</string>
  <key>CFBundleDisplayName</key>     <string>MillenAI</string>
  <key>CFBundleIdentifier</key>      <string>com.millen.millenai</string>
  <key>CFBundleVersion</key>         <string>${APP_BUILD}</string>
  <key>CFBundleShortVersionString</key> <string>${APP_VERSION}</string>
  <key>CFBundleExecutable</key>      <string>MillenAI</string>
  <key>CFBundleIconFile</key>        <string>MillenAI</string>
  <key>CFBundlePackageType</key>     <string>APPL</string>
  <key>NSHighResolutionCapable</key> <true/>
  <key>LSMinimumSystemVersion</key>  <string>11.0</string>
  <key>NSMicrophoneUsageDescription</key> <string>MillenAI uses the microphone for voice input — audio never leaves this Mac.</string>
</dict>
</plist>
PLIST

# --- launcher: self-bootstrapping, so the .app works on a fresh Mac.
# On first run it builds a private venv in ~/Library/Application Support
# and pip-installs the engine deps (needs internet, a few minutes).
# NOTE: single-quoted heredoc — nothing here is expanded at build time.
cat > "$APP/Contents/MacOS/MillenAI" <<'LAUNCH'
#!/bin/zsh
DIR="$(cd "$(dirname "$0")" && pwd)"

# We only get here if the user already cleared Gatekeeper once, so strip the
# download flag from our own bundle: later launches, and drag-and-drop
# updates onto this same bundle, then open without prompting again.
# (Nothing can do this *before* first launch — that is the whole point of
# quarantine — so the DMG still has to explain the one-time approval.)
xattr -dr com.apple.quarantine "$DIR/../.." 2>/dev/null || true

SUPPORT="$HOME/Library/Application Support/MillenAI"
LOGDIR="$HOME/Library/Logs/MillenAI"
VENV="$SUPPORT/venv"
PY="$VENV/bin/python3"
mkdir -p "$SUPPORT" "$LOGDIR"

if [[ ! -x "$PY" ]] || ! "$PY" -c "import webview" 2>/dev/null; then
  /usr/bin/osascript -e 'display notification "First run: setting up the AI engine (a few minutes)…" with title "MillenAI"' 2>/dev/null || true
  BASE="$(command -v python3 || echo /usr/bin/python3)"
  {
    "$BASE" -m venv "$VENV" &&
    "$VENV/bin/pip" install --upgrade pip &&
    "$VENV/bin/pip" install pywebview ddgs psutil
  } >> "$LOGDIR/bootstrap.log" 2>&1
  # MLX exists only for Apple silicon; Intel Macs run the models on Ollama,
  # which the app downloads for itself on first use
  if [[ "$(uname -m)" == "arm64" ]]; then
    "$VENV/bin/pip" install mlx-lm mlx-whisper >> "$LOGDIR/bootstrap.log" 2>&1 || true
  fi
  if [[ ! -x "$PY" ]] || ! "$PY" -c "import webview" 2>/dev/null; then
    /usr/bin/osascript -e 'display dialog "MillenAI could not set up its Python engine. Install the Apple Command Line Tools (run: xcode-select --install), check your internet connection, and open MillenAI again." buttons {"OK"} with title "MillenAI"' 2>/dev/null || true
    exit 1
  fi
fi

exec "$PY" "$DIR/../Resources/millenai.py"
LAUNCH
chmod +x "$APP/Contents/MacOS/MillenAI"

touch "$APP"

# --- ad-hoc signature (free, no Apple account).
# This does NOT satisfy Gatekeeper — only a paid Developer ID + notarization
# does — but it gives the bundle a stable identity, avoids the harsher
# "app is damaged" rejection, and keeps Apple silicon happy.
codesign --force --sign - "$APP" 2>/dev/null \
  && echo "  ad-hoc signed" \
  || echo "  ! ad-hoc signing failed (app still runs; see README)"

echo ""
echo "✓ built $APP"
echo "  drag it into /Applications."
echo "  first launch on another Mac: System Settings ▸ Privacy & Security"
echo "  ▸ scroll down ▸ 'Open Anyway'  (macOS 15+ removed right-click→Open)"
