#!/usr/bin/env python3
"""Concorde share card — the 1200x630 og:image.

Same parts as the hero, composed for a small card: the wing, the wordmark
in Michroma under the silver ramp, the brand line, and a quiet starfield.
Drawn 2x and downsampled; PIL has no antialiasing of its own.

Text is baked, so this file is the one place the card's wording lives —
regenerate rather than hand-editing the PNG.
"""
import math
import os
import random
import sys

from PIL import Image, ImageDraw, ImageFont

OUT = sys.argv[1] if len(sys.argv) > 1 else "og.png"
FONT = "/Users/patrickmiller/Code/concorde-vpn/fonts/Michroma-Regular.ttf"

X = 2
W, H = 1200 * X, 630 * X
BG = (16, 16, 19)
FAINT = (142, 142, 142)
STOPS = [(0.00, (0x78, 0x7e, 0x89)),
         (0.55, (0xb7, 0xbc, 0xc6)),
         (1.00, (0xf4, 0xf5, 0xf8))]

VB = (2.0, 2.3, 19.6, 16.4)
LINES = [(3.2, 17.5, 20.4, 3.5),
         (7.5, 17.5, 20.4, 7.0),
         (11.8, 17.5, 20.4, 10.5),
         (16.1, 17.5, 20.4, 14.0),
         (19.3, 17.5, 20.4, 16.6)]
STROKE = 1.6          # the hero's weight, not the app's 2.4


def grad_rgb(t):
    t = max(0.0, min(1.0, t))
    for i in range(len(STOPS) - 1):
        t0, c0 = STOPS[i]
        t1, c1 = STOPS[i + 1]
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return tuple(int(c0[j] + (c1[j] - c0[j]) * f) for j in range(3))
    return STOPS[-1][1]


def ramp(w, h):
    """Horizontal silver ramp — the wordmark's own --silver treatment."""
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for x in range(w):
        d.line([(x, 0), (x, h)], fill=grad_rgb(x / max(1, w - 1)))
    return img


def diag_ramp(w, h):
    """Bottom-left steel to top-right silver, as the wing's wgrad."""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = grad_rgb((x / w + (1.0 - y / h)) / 2.0)
    return img


def starfield(img):
    """Sparse and dim: atmosphere, never a texture that fights the type."""
    rnd = random.Random(1969)          # Concorde's first flight
    d = ImageDraw.Draw(img, "RGBA")
    for _ in range(150):
        x, y = rnd.uniform(0, W), rnd.uniform(0, H)
        r = rnd.uniform(0.8, 2.2) * X
        a = int(rnd.uniform(18, 70))
        tone = rnd.choice([(245, 246, 248), (200, 204, 213), (154, 160, 172)])
        d.ellipse([x - r, y - r, x + r, y + r], fill=tone + (a,))
    return img


def center_glow(img):
    """The hero's radial lift, so the card isn't a flat black rectangle."""
    g = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(g)
    cx, cy, rad = W / 2, H * 0.42, W * 0.42
    steps = 90
    for i in range(steps, 0, -1):
        t = i / steps
        r = rad * t
        d.ellipse([cx - r, cy - r, cx + r, cy + r],
                  fill=int(26 * (1 - t) ** 2))
    img.paste(Image.new("RGB", (W, H), (200, 204, 213)), (0, 0), g)
    return img


def wing(size):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    vx, vy, vw, vh = VB
    k = size / max(vw, vh)
    ox = (size - vw * k) / 2.0
    oy = (size - vh * k) / 2.0

    def P(a, b):
        return (ox + (a - vx) * k, oy + (b - vy) * k)

    w = STROKE * k
    for (x1, y1, x2, y2) in LINES:
        a, b = P(x1, y1), P(x2, y2)
        d.line([a, b], fill=255, width=max(1, int(round(w))))
        for (cx, cy) in (a, b):
            d.ellipse([cx - w / 2, cy - w / 2, cx + w / 2, cy + w / 2], fill=255)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    layer.paste(diag_ramp(size, size), (0, 0), m)
    return layer


def tracked(draw_img, text, font, track, y, fill=None, ramp_img=None,
            bold_from=None, stroke=0):
    """Michroma with letter-spacing, centred. `bold_from` fakes the heavier
    suffix of the product lockups the same way the site does (synthetic
    weight via stroke)."""
    widths, total = [], 0.0
    for i, ch in enumerate(text):
        sw = stroke if (bold_from is not None and i >= bold_from) else 0
        bb = font.getbbox(ch, stroke_width=sw)
        cw = bb[2] - bb[0]
        widths.append((ch, cw, bb, sw))
        total += cw + track
    total -= track
    x = (W - total) / 2.0
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    for ch, cw, bb, sw in widths:
        md.text((x - bb[0], y), ch, font=font, fill=255,
                stroke_width=sw, stroke_fill=255)
        x += cw + track
    if ramp_img is not None:
        draw_img.paste(ramp_img, (0, 0), mask)
    else:
        draw_img.paste(Image.new("RGB", (W, H), fill), (0, 0), mask)
    return total


def segments(img, segs, font, track, y, fill):
    """One centred run built from (text, stroke) pairs, so the product
    lockups keep their heavier suffix — CONCORDE + **AI** — the way the
    brand requires everywhere the composite appears."""
    items, total = [], 0.0
    for text, sw in segs:
        for ch in text:
            bb = font.getbbox(ch, stroke_width=sw)
            cw = bb[2] - bb[0]
            items.append((ch, cw, bb, sw))
            total += cw + track
    total -= track
    x = (W - total) / 2.0
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    for ch, cw, bb, sw in items:
        md.text((x - bb[0], y), ch, font=font, fill=255,
                stroke_width=sw, stroke_fill=255)
        x += cw + track
    img.paste(Image.new("RGB", (W, H), fill), (0, 0), mask)


def main():
    img = Image.new("RGB", (W, H), BG)
    center_glow(img)
    starfield(img)

    # wing
    ws = int(150 * X)
    img.paste(wing(ws), (int((W - ws) / 2), int(96 * X)), wing(ws))

    # wordmark, under the silver ramp
    f_mark = ImageFont.truetype(FONT, int(76 * X))
    tracked(img, "CONCORDE", f_mark, int(76 * X * 0.15), int(268 * X),
            ramp_img=ramp(W, H))

    # brand line
    f_slog = ImageFont.truetype(FONT, int(17 * X))
    tracked(img, "FLY CONCORDE, FLY.", f_slog, int(17 * X * 0.34),
            int(372 * X), fill=FAINT)

    # hairline + the two products, so the card says what the link is
    d = ImageDraw.Draw(img)
    d.line([(W * 0.34, 468 * X), (W * 0.66, 468 * X)], fill=(38, 39, 44),
           width=max(1, X))
    f_prod = ImageFont.truetype(FONT, int(19 * X))
    b = max(1, int(1.1 * X))          # synthetic weight for the suffixes
    segments(img, [("CONCORDE", 0), ("AI", b), ("   \u00b7   ", 0),
                   ("CONCORDE", 0), ("VPN", b)],
             f_prod, int(19 * X * 0.15), int(516 * X), (180, 180, 180))

    img.resize((W // X, H // X), Image.LANCZOS).save(OUT)
    print("wrote %s (%dx%d)" % (OUT, W // X, H // X))


if __name__ == "__main__":
    main()
