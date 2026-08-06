"""MillenAI app icon (5.3: greyscale).

The artwork fills the FULL Apple icon grid — an 824x824 squircle on
the 1024 canvas (margins 100, corner radius ~185), the same envelope
every stock macOS icon uses; anything bigger gets shrunk by the OS
and reads SMALLER in the Dock (learned in 5.2). Inside: charcoal
night, faint stars, and a brushed-silver M — the rainbow version read
ridiculous next to real apps (5.3, per Patrick).
"""
import math
import os
import random
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = "/Users/patrickmiller/My Drive/Downloads/files"
SCRATCH = os.path.dirname(os.path.abspath(__file__))

S = 1024
M0 = 100            # Apple grid margin
SQ = S - 2 * M0     # 824 squircle
RAD = 185           # Apple's corner radius at this scale

# 5.3, per Patrick ("maybe a greyscale M" — the rainbow read
# ridiculous in the Dock): a silver ramp, bright at the top-left,
# steel at the bottom-right, like brushed metal catching window light
PALETTE = [(246, 247, 250), (228, 231, 238), (204, 208, 218),
           (176, 181, 194), (150, 156, 170), (128, 134, 148),
           (112, 118, 132)]


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def palette_at(t):
    t = max(0.0, min(1.0, t)) * (len(PALETTE) - 1)
    i = min(int(t), len(PALETTE) - 2)
    return lerp(PALETTE[i], PALETTE[i + 1], t - i)


def squircle_mask():
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((M0, M0, S - M0, S - M0), radius=RAD, fill=255)
    return m


def background():
    """Deep navy vertical gradient + bottom city glow + stars + aurora."""
    bg = Image.new("RGB", (S, S))
    top, bot = (14, 15, 19), (32, 34, 42)
    px = bg.load()
    for y in range(S):
        c = lerp(top, bot, y / S)
        for x in range(S):
            px[x, y] = c
    # bottom city glow — warm amber breathing up from the horizon
    glow = Image.new("RGB", (S, S), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((S * 0.02, S * 0.84, S * 0.98, S * 1.45),
               fill=(46, 48, 58))
    glow = glow.filter(ImageFilter.GaussianBlur(95))
    bg = Image.blend(bg, Image.blend(bg, glow, 1.0).point(lambda v: v), 0.0)
    bg = ImageChops_add(bg, glow)
    # stars
    rnd = random.Random(57)
    sd = ImageDraw.Draw(bg)
    for _ in range(130):
        x, y = rnd.uniform(M0, S - M0), rnd.uniform(M0, S * 0.62)
        r = rnd.choice((1, 1, 1, 2))
        a = rnd.randint(40, 140)
        sd.ellipse((x - r, y - r, x + r, y + r), fill=(a, a, min(255, a + 20)))
    # aurora band — soft rainbow strip on a diagonal, heavily blurred
    au = Image.new("RGB", (S, S), (0, 0, 0))
    ad = ImageDraw.Draw(au)
    n = 260
    for k in range(n):
        t = k / (n - 1)
        c = palette_at(t)
        x0 = -S * 0.2 + t * S * 1.4
        ad.line([(x0, S * 0.98), (x0 + S * 0.45, -S * 0.1)],
                fill=c, width=7)
    au = au.filter(ImageFilter.GaussianBlur(90))
    au = au.point(lambda v: int(v * 0.05))
    bg = ImageChops_add(bg, au)
    # vignette — corners fall away, the center breathes
    vig = Image.new("L", (S, S), 0)
    vd = ImageDraw.Draw(vig)
    vd.ellipse((-S * 0.25, -S * 0.25, S * 1.25, S * 1.25), fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(160))
    black = Image.new("RGB", (S, S), (0, 0, 0))
    inv = vig.point(lambda v: int((255 - v) * 0.36))
    bg.paste(black, (0, 0), inv)
    return bg


def ImageChops_add(a, b):
    from PIL import ImageChops
    return ImageChops.add(a, b)


def m_glyph_mask(font_path, index, target_w):
    """The letter M rendered huge, returned as an L-mode mask."""
    size = 900
    font = ImageFont.truetype(font_path, size, index=index)
    tmp = Image.new("L", (S * 2, S * 2), 0)
    d = ImageDraw.Draw(tmp)
    d.text((S, S), "M", font=font, fill=255, anchor="mm")
    box = tmp.getbbox()
    glyph = tmp.crop(box)
    w, h = glyph.size
    scale = target_w / w
    return glyph.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def build():
    mask = squircle_mask()
    art = background()

    # the M — Condensed Black, ~62% of the tile: big enough to own the
    # squircle, small enough to keep the breathing room stock icons have
    glyph = m_glyph_mask("/System/Library/Fonts/HelveticaNeue.ttc", 9,
                         int(SQ * 0.66))
    gw, gh = glyph.size
    gx = (S - gw) // 2
    gy = (S - gh) // 2 - 14          # a touch above optical center

    # silver fill, diagonal
    grad = Image.new("RGB", (gw, gh))
    gp = grad.load()
    for y in range(gh):
        for x in range(gw):
            t = (x / gw) * 0.82 + (y / gh) * 0.18
            gp[x, y] = palette_at(t)

    # glow behind the glyph: a tight bright halo plus a wide soft bloom,
    # both carrying the glyph's own rainbow so the light reads as ITS
    glow_src = Image.new("RGB", (S, S), (0, 0, 0))
    glow_src.paste(grad, (gx, gy), glyph)
    tight = glow_src.filter(ImageFilter.GaussianBlur(18))
    wide = glow_src.filter(ImageFilter.GaussianBlur(70))
    art = ImageChops_add(art, tight.point(lambda v: int(v * 0.30)))
    art = ImageChops_add(art, wide.point(lambda v: int(v * 0.20)))

    # the M itself
    art.paste(grad, (gx, gy), glyph)

    # rim light: a white copy nudged up shows only along the top edges —
    # the cheap bevel that makes the glyph sit IN the scene, not on it
    from PIL import ImageChops
    rim = Image.new("L", (S, S), 0)
    rim.paste(glyph, (gx, gy - 5))
    body = Image.new("L", (S, S), 0)
    body.paste(glyph, (gx, gy))
    rim = ImageChops.subtract(rim, body)
    rim = rim.filter(ImageFilter.GaussianBlur(1.4))
    rim = rim.point(lambda v: int(v * 0.55))
    white_rim = Image.new("RGB", (S, S), (255, 255, 255))
    art.paste(white_rim, (0, 0), rim)

    # glass: inner top highlight, fading out by ~18% down
    hl = Image.new("L", (S, S), 0)
    hp = hl.load()
    for y in range(M0, int(S * 0.30)):
        a = int(46 * max(0.0, 1 - (y - M0) / (S * 0.30 - M0)))
        for x in range(S):
            hp[x, y] = a
    white = Image.new("RGB", (S, S), (255, 255, 255))
    art.paste(white, (0, 0), hl)

    # assemble onto transparency + hairline border
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(art, (0, 0), mask)
    bd = ImageDraw.Draw(out)
    bd.rounded_rectangle((M0, M0, S - M0, S - M0), radius=RAD,
                         outline=(255, 255, 255, 56), width=2)
    return out


def export(icon):
    prev = os.path.join(SCRATCH, "icon_preview.png")
    icon.resize((512, 512), Image.LANCZOS).save(prev)
    iconset = os.path.join(SCRATCH, "MillenAI.iconset")
    os.makedirs(iconset, exist_ok=True)
    for pt in (16, 32, 128, 256, 512):
        for mult in (1, 2):
            px = pt * mult
            name = ("icon_%dx%d.png" % (pt, pt) if mult == 1
                    else "icon_%dx%d@2x.png" % (pt, pt))
            icon.resize((px, px), Image.LANCZOS).save(
                os.path.join(iconset, name))
    icns = os.path.join(SCRATCH, "MillenAI.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns],
                   check=True)
    # windows ico straight from the same art
    ico = os.path.join(SCRATCH, "MillenAI.ico")
    icon.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
                          (128, 128), (256, 256)])
    print("preview:", prev)
    print("icns:", icns, os.path.getsize(icns))
    print("ico:", ico, os.path.getsize(ico))


if __name__ == "__main__":
    export(build())
