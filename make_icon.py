"""Concorde app icon (6.1: touching full-bleed greyscale bars).

The artwork fills the FULL Apple icon grid — an 824x824 squircle on
the 1024 canvas (margins 100, corner radius ~185); anything bigger
gets shrunk by the OS and reads SMALLER in the Dock (learned in 5.2).
Inside: four parallel 45-degree capsules in the lower-right triangular
half, purple sweeping to teal, on quiet charcoal. Drawn 2x and
downsampled because PIL draws without antialiasing.
"""
import os
import subprocess

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))

S = 1024
M0 = 100            # Apple grid margin
SQ = S - 2 * M0     # 824 squircle
RAD = 185           # Apple's corner radius at this scale

# 6.1: greyscale — bright silver at the diagonal, steel at the corner
G_STOPS = [(242, 243, 246), (183, 188, 198), (120, 126, 137)]
# viewBox-120 geometry straight from the SVG in millenai.py
BARS = [(18, 62, 14, 40), (39, 44, 14, 58), (60, 30, 14, 72),
        (81, 52, 14, 50)]
DOT = (95, 24, 7)          # cx, cy, r


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def grad_at(t):
    t = max(0.0, min(1.0, t)) * (len(G_STOPS) - 1)
    i = min(int(t), len(G_STOPS) - 2)
    return lerp(G_STOPS[i], G_STOPS[i + 1], t - i)


def squircle_mask():
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((M0, M0, S - M0, S - M0), radius=RAD, fill=255)
    return m


def bars_layer():
    """Diagonal bars (5.3.7, per Patrick): four parallel capsules at
    45 degrees filling the LOWER-RIGHT triangular half of the tile.
    Centres march down the main diagonal; each bar runs along the
    anti-diagonal ("/") and shortens toward the corner, so the group
    reads as a triangle. Purple nearest the centre, teal at the
    corner — the same sweep as the old upright mark. Drawn 2x
    (line + end circles = capsule) and LANCZOS-downsampled.
    """
    import math
    X = 2
    S2 = S * X
    lay = Image.new("RGBA", (S2, S2), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    u = (math.sqrt(.5), -math.sqrt(.5))     # bar axis: up-right "/"
    v = (math.sqrt(.5), math.sqrt(.5))      # step: toward bottom-right
    # 6.1, per Patrick: bars TOUCH (step == width) and STRETCH past the
    # tile — the squircle mask at composite time crops them flush to
    # the icon edge, so the stripes bleed edge-to-edge with no margin.
    w = 118 * X
    n = 6                                    # covers centre -> corner
    L = int(S * 1.6) * X                     # crosses the whole tile
    for k in range(n):
        c = grad_at(k / (n - 1))
        off = (w / 2 + w * k) / X            # touching: no gap
        cx = S2 / 2 + v[0] * off * X
        cy = S2 / 2 + v[1] * off * X
        x0, y0 = cx - u[0] * L / 2, cy - u[1] * L / 2
        x1, y1 = cx + u[0] * L / 2, cy + u[1] * L / 2
        d.line([(x0, y0), (x1, y1)], fill=c + (255,), width=w)
    return lay.resize((S, S), Image.LANCZOS)


def build():
    mask = squircle_mask()

    # quiet charcoal, faintly darker at the bottom — flat like the app's
    # About panel, no starfield, no theatrics
    art = Image.new("RGB", (S, S))
    px = art.load()
    top, bot = (46, 48, 54), (33, 34, 39)
    for y in range(S):
        c = lerp(top, bot, y / S)
        for x in range(S):
            px[x, y] = c

    mark = bars_layer()
    mw, mh = mark.size
    mx = (S - mw) // 2
    my = (S - mh) // 2

    # a soft violet-teal bloom behind the mark so it sits in light
    glow = Image.new("RGB", (S, S), (0, 0, 0))
    glow.paste(mark.convert("RGB"), (mx, my), mark.split()[3])
    glow = glow.filter(ImageFilter.GaussianBlur(60))
    from PIL import ImageChops
    art = ImageChops.add(art, glow.point(lambda v: int(v * 0.30)))

    art.paste(mark.convert("RGB"), (mx, my), mark.split()[3])

    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(art, (0, 0), mask)
    bd = ImageDraw.Draw(out)
    bd.rounded_rectangle((M0, M0, S - M0, S - M0), radius=RAD,
                         outline=(255, 255, 255, 40), width=2)
    return out


def export(icon):
    prev = os.path.join(HERE, "icon_preview.png")
    icon.resize((512, 512), Image.LANCZOS).save(prev)
    iconset = os.path.join(HERE, "MillenAI.iconset")
    os.makedirs(iconset, exist_ok=True)
    for pt in (16, 32, 128, 256, 512):
        for mult in (1, 2):
            px = pt * mult
            name = ("icon_%dx%d.png" % (pt, pt) if mult == 1
                    else "icon_%dx%d@2x.png" % (pt, pt))
            icon.resize((px, px), Image.LANCZOS).save(
                os.path.join(iconset, name))
    icns = os.path.join(HERE, "MillenAI.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns],
                   check=True)
    ico = os.path.join(HERE, "MillenAI.ico")
    icon.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
                          (128, 128), (256, 256)])
    print("preview:", prev)
    print("icns:", icns, os.path.getsize(icns))
    print("ico:", ico, os.path.getsize(ico))


if __name__ == "__main__":
    export(build())
