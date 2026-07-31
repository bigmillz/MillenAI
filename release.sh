#!/bin/zsh
# Publish a new MillenAI build to GitHub so existing installs can self-update.
#
#   ./release.sh 5 "V1 Beta 5"
#
# Bumps APP_BUILD/APP_VERSION in millenai.py, rebuilds the app + DMG, then
# creates a GitHub Release tagged v<build> with the .dmg attached. Running
# copies of MillenAI compare that tag against their own APP_BUILD and offer
# the update. Needs the GitHub CLI:  brew install gh && gh auth login
set -e
cd "$(dirname "$0")"

BUILD="$1"; LABEL="$2"
if [[ -z "$BUILD" || -z "$LABEL" ]]; then
  echo "usage: ./release.sh <build-number> \"<version label>\""
  echo "   eg: ./release.sh 5 \"V1 Beta 5\""
  exit 1
fi
if ! command -v gh >/dev/null; then
  echo "gh not found — install with: brew install gh && gh auth login"; exit 1
fi

echo "→ bumping to build $BUILD ($LABEL)"
python3 - "$BUILD" "$LABEL" <<'PY'
import pathlib, re, sys
build, label = sys.argv[1], sys.argv[2]
p = pathlib.Path("millenai.py"); s = p.read_text()
s = re.sub(r'APP_VERSION = "[^"]*"', 'APP_VERSION = "%s"' % label, s, count=1)
s = re.sub(r'APP_BUILD = \d+', 'APP_BUILD = %s' % build, s, count=1)
p.write_text(s)
PY

# the DMG name follows the version label
python3 - "$LABEL" <<'PY'
import pathlib, re, sys
label = sys.argv[1]
p = pathlib.Path("build_dmg.sh"); s = p.read_text()
s = re.sub(r'MillenAI V1 Beta \d+', "MillenAI %s" % label, s)
p.write_text(s)
PY

echo "→ building"
./build_dmg.sh >/dev/null
DMG="MillenAI ${LABEL}.dmg"
[[ -f "$DMG" ]] || { echo "expected $DMG but it wasn't built"; exit 1; }

echo "→ publishing v$BUILD"
git add -A && git commit -m "Release $LABEL (build $BUILD)" || true
git push origin HEAD
gh release create "v$BUILD" "$DMG" \
  --title "$LABEL" \
  --notes "MillenAI $LABEL. Open the app and it will offer this update automatically."

echo ""
echo "✓ published v$BUILD — existing installs will offer it within the hour."
