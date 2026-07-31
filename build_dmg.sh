#!/bin/zsh
# Packages MillenAI into a styled, shareable disk image: MillenAI V1 Beta 5.dmg
# Finder shows a custom starfield background with a drag-to-install arrow.
set -e
cd "$(dirname "$0")"

./build_macos_app.sh

VENV="$HOME/Library/Application Support/MillenAI/venv"
"$VENV/bin/pip" install --quiet pillow

STAGE="$(mktemp -d)/MillenAI"
mkdir -p "$STAGE/.background"
cp -R MillenAI.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"

# --- background art (1320x800 @144dpi -> 660x400 pts, retina-sharp)
"$VENV/bin/python3" - "$STAGE/.background/bg.png" <<'PYEOF'
import random
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

W, H = 1320, 800
top, bot = (13, 15, 22), (19, 24, 41)
img = Image.new("RGB", (W, H))
d = ImageDraw.Draw(img)
for y in range(H):
    t = y / H
    d.line([(0, y), (W, y)],
           fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bot)))

# soft corner glows (periwinkle / teal)
glow = Image.new("RGB", (W, H), (0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.ellipse([-260, -300, 560, 380], fill=(26, 32, 74))
gd.ellipse([880, 480, 1560, 1060], fill=(10, 44, 44))
img = ImageChops.add(img, glow.filter(ImageFilter.GaussianBlur(160)))

img = img.convert("RGBA")
overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
od = ImageDraw.Draw(overlay)

# starfield with a few warp streaks
random.seed(7)
palette = [(228, 232, 244), (125, 143, 255), (95, 212, 196), (179, 193, 255)]
for _ in range(150):
    x, y = random.uniform(0, W), random.uniform(0, H)
    r = random.uniform(0.8, 2.6)
    c = random.choice(palette)
    od.ellipse([x - r, y - r, x + r, y + r],
               fill=c + (int(random.uniform(60, 220)),))
for _ in range(22):
    x, y = random.uniform(0, W), random.uniform(0, H)
    dx, dy = (x - W / 2), (y - H / 2)
    n = max((dx * dx + dy * dy) ** 0.5, 1)
    ln = random.uniform(26, 70)
    c = random.choice(palette)
    od.line([x, y, x + dx / n * ln, y + dy / n * ln],
            fill=c + (int(random.uniform(28, 80)),), width=2)

def font(path, size, index=0):
    try:
        return ImageFont.truetype(path, size, index=index)
    except Exception:
        return ImageFont.load_default()

big = font("/System/Library/Fonts/Helvetica.ttc", 92, index=1)  # bold
sub = font("/System/Library/Fonts/Helvetica.ttc", 30)
mono = font("/System/Library/Fonts/Menlo.ttc", 24)

def centered(y, text, f, fill):
    w = od.textlength(text, font=f)
    od.text(((W - w) / 2, y), text, font=f, fill=fill)

# wordmark: MillenAI + periwinkle "V1 Beta 4"
name, tag = "MillenAI ", "V1 Beta 4"
nw = od.textlength(name, font=big)
tw = od.textlength(tag, font=big)
x0 = (W - nw - tw) / 2
od.text((x0, 64), name, font=big, fill=(228, 232, 244, 255))
od.text((x0 + nw, 64), tag, font=big, fill=(125, 143, 255, 255))
centered(178, "your models. your mac. zero cloud.", sub, (143, 151, 173, 235))

# install help. macOS 15+ removed the right-click→Open bypass, so an
# unnotarized app MUST be allowed from System Settings the first time.
small = font("/System/Library/Fonts/Menlo.ttc", 21)
centered(668, "1. drag MillenAI into Applications, then open it once",
         small, (120, 128, 150, 235))
centered(704, "2. macOS will block it — that is expected for a free app",
         small, (120, 128, 150, 235))
centered(740, "3. System Settings ▸ Privacy & Security ▸ Open Anyway",
         small, (125, 143, 255, 245))

# gradient drag arrow between the two icon slots (icons at 165pt / 495pt)
ay, x1, x2 = 396, 500, 790
c1, c2 = (125, 143, 255), (95, 212, 196)
steps = 60
for i in range(steps):
    t0, t1 = i / steps, (i + 1) / steps
    c = tuple(int(a + (b - a) * t0) for a, b in zip(c1, c2))
    od.line([x1 + (x2 - x1) * t0, ay, x1 + (x2 - x1) * t1 + 2, ay],
            fill=c + (255,), width=14)
od.polygon([(x2 + 52, ay), (x2 - 6, ay - 34), (x2 - 6, ay + 34)],
           fill=c2 + (255,))

# soft glow behind the arrow
halo = overlay.filter(ImageFilter.GaussianBlur(14))
img = Image.alpha_composite(img, halo)
img = Image.alpha_composite(img, overlay)
img.convert("RGB").save(sys.argv[1], dpi=(144, 144))
print("background written")
PYEOF

# --- writable image first, style it via Finder, then compress
rm -f "MillenAI V1 Beta 5.dmg" MillenAI.dmg MillenAI-rw.dmg
hdiutil create -volname "MillenAI V1 Beta 5" -srcfolder "$STAGE" -ov -format UDRW \
  -quiet MillenAI-rw.dmg
hdiutil attach -readwrite -noverify -noautoopen -quiet MillenAI-rw.dmg

osascript <<'OSA'
tell application "Finder"
  tell disk "MillenAI V1 Beta 5"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set pathbar visible of container window to false
    set vo to the icon view options of container window
    set arrangement of vo to not arranged
    set icon size of vo to 128
    set text size of vo to 13
    set background picture of vo to file ".background:bg.png"
    set position of item "MillenAI.app" of container window to {165, 210}
    set position of item "Applications" of container window to {495, 210}
    update without registering applications
    delay 1
    -- set bounds twice with a nudge: Finder only persists the size if it
    -- registers a change while the window is frontmost
    set the bounds of container window to {200, 120, 859, 547}
    delay 1
    set the bounds of container window to {200, 120, 860, 548}
    update without registering applications
    delay 2
    close
    delay 1
  end tell
end tell
OSA

# volume icon LAST — the Finder styling session above deletes
# .VolumeIcon.icns and clears the custom-icon flag if they exist earlier
if [[ -f MillenAI.icns ]]; then
  cp MillenAI.icns "/Volumes/MillenAI V1 Beta 5/.VolumeIcon.icns"
  SetFile -a C "/Volumes/MillenAI V1 Beta 5" 2>/dev/null \
    || xcrun SetFile -a C "/Volumes/MillenAI V1 Beta 5" 2>/dev/null \
    || echo "  (SetFile unavailable — volume icon flag skipped)"
fi

sync
hdiutil detach -quiet "/Volumes/MillenAI V1 Beta 5"
hdiutil convert -quiet MillenAI-rw.dmg -format UDZO -o "MillenAI V1 Beta 5.dmg"
rm -f MillenAI-rw.dmg
rm -rf "$(dirname "$STAGE")"

echo ""
echo "✓ built MillenAI V1 Beta 5.dmg ($(du -h "MillenAI V1 Beta 5.dmg" | cut -f1 | tr -d ' '))"
