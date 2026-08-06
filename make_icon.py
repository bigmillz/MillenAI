"""MillenAI app icon (5.3.1: the gradient-bars mark, reverted by ask).

The artwork fills the FULL Apple icon grid — an 824x824 squircle on
the 1024 canvas (margins 100, corner radius ~185), the same envelope
every stock macOS icon uses; anything bigger gets shrunk by the OS
and reads SMALLER in the Dock (learned in 5.2). Inside: the About
panel's bar-chart mark — four rounded bars sweeping purple to teal
with the teal dot — on quiet charcoal. Bars are drawn 2x and
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

# the About-panel SVG's gradient: #8b5cf6 -> #7d8fff -> #4cc9e0
G_STOPS = [(139, 92, 246), (125, 143, 255), (76, 201, 224)]
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
    """The mark, rendered 2x and downsampled, on transparency.

    Each bar takes its colour from where its centre sits along the
    group's sweep — matching how the mark reads in the app (left bars
    violet, right bars toward teal) — with a slight vertical lift so
    the tops feel lit.
    """
    X = 2                       # supersample factor
    scale = (SQ * 0.60) / 120.0   # mark spans ~60% of the tile
    w = int(120 * scale * X)
    lay = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    for (bx, by, bw, bh) in BARS:
        cx_norm = (bx + bw / 2 - 18) / (95 - 18)   # 0 at first bar, 1 at dot
        base = grad_at(cx_norm * 0.9)
        top = lerp(base, (255, 255, 255), 0.18)
        x0, y0 = bx * scale * X, by * scale * X
        x1, y1 = (bx + bw) * scale * X, (by + bh) * scale * X
        r = 6 * scale * X
        # vertical mini-gradient inside the bar: lit top, base bottom
        n = max(1, int(y1 - y0))
        bar = Image.new("RGBA", (int(x1 - x0) + 2, n + 2), (0, 0, 0, 0))
        bp = bar.load()
        for yy in range(n):
            c = lerp(top, base, yy / n)
            for xx in range(bar.size[0]):
                bp[xx, yy] = c + (255,)
        m = Image.new("L", bar.size, 0)
        ImageDraw.Draw(m).rounded_rectangle(
            (0, 0, bar.size[0] - 2, n), radius=r, fill=255)
        lay.paste(bar, (int(x0), int(y0)), m)
    cx, cy, r = DOT
    d.ellipse(((cx - r) * scale * X, (cy - r) * scale * X,
               (cx + r) * scale * X, (cy + r) * scale * X),
              fill=(76, 201, 224, 255))
    out_px = int(120 * scale)
    return lay.resize((out_px, out_px), Image.LANCZOS)


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
