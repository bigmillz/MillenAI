#!/bin/zsh
# Publish a new MillenAI release to GitHub so existing installs self-update.
#
#   ./release.sh patch     bug fix        1.0.1 -> 1.0.2
#   ./release.sh minor     new feature    1.0.1 -> 1.1.0
#   ./release.sh major     rewrite        1.0.1 -> 2.0.0
#   ./release.sh 1.4.2     explicit version
#
# The build number is a separate monotonic counter that always increments —
# it is what the in-app updater compares, so the marketing version can move
# however you like without breaking updates.
#
# Needs the GitHub CLI:  brew install gh && gh auth login
set -e
cd "$(dirname "$0")"

ARG="$1"
if [[ -z "$ARG" ]]; then
  echo "usage: ./release.sh <patch|minor|major|X.Y.Z>"
  exit 1
fi
if ! command -v gh >/dev/null; then
  echo "gh not found — install with: brew install gh && gh auth login"; exit 1
fi

# --- work out the next version + build from millenai.py
read -r VERSION BUILD <<<"$(python3 - "$ARG" <<'PY'
import re, sys, pathlib
arg = sys.argv[1]
src = pathlib.Path("millenai.py").read_text()
cur = re.search(r'APP_VERSION = "([^"]+)"', src).group(1)
build = int(re.search(r"APP_BUILD = (\d+)", src).group(1)) + 1

if arg in ("patch", "minor", "major"):
    parts = [int(x) for x in re.findall(r"\d+", cur)]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[:3]
    if arg == "patch":
        patch += 1
    elif arg == "minor":
        minor, patch = minor + 1, 0
    else:
        major, minor, patch = major + 1, 0, 0
    version = f"{major}.{minor}.{patch}"
else:
    if not re.fullmatch(r"\d+\.\d+\.\d+", arg):
        sys.exit("version must look like 1.2.3")
    version = arg
print(version, build)
PY
)"
[[ -n "$VERSION" ]] || { echo "could not work out the version"; exit 1; }

echo "→ $VERSION (build $BUILD)"
python3 - "$VERSION" "$BUILD" <<'PY'
import pathlib, re, sys
version, build = sys.argv[1], sys.argv[2]
p = pathlib.Path("millenai.py"); s = p.read_text()
s = re.sub(r'APP_VERSION = "[^"]*"', 'APP_VERSION = "%s"' % version, s, count=1)
s = re.sub(r"APP_BUILD = \d+", "APP_BUILD = %s" % build, s, count=1)
p.write_text(s)
PY

echo "→ building macOS"
./build_dmg.sh >/dev/null
DMG="MillenAI ${VERSION}.dmg"   # build_dmg.sh derives this from millenai.py
[[ -f "$DMG" ]] || { echo "expected $DMG but it wasn't built"; exit 1; }

echo "→ building Windows"
./build_windows.sh >/dev/null
ZIP="MillenAI-${VERSION}-Windows.zip"
[[ -f "$ZIP" ]] || { echo "expected $ZIP but it wasn't built"; exit 1; }

echo "→ publishing v$BUILD"
git add -A && git commit -m "Release $VERSION (build $BUILD)" || true
git push origin HEAD
gh release create "v$BUILD" "$DMG" "$ZIP" \
  --title "$VERSION" \
  --notes "MillenAI $VERSION.

**macOS** — download the .dmg. Existing installs update themselves.

**Windows** — download the .zip, unzip anywhere, run MillenAI.bat. Needs
Python 3.10+ from python.org. On an NVIDIA machine it uses CUDA
automatically; no setup, nothing extra to install."

echo ""
echo "✓ published $VERSION — existing installs will offer it within the hour."
