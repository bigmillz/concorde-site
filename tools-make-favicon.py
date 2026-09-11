#!/usr/bin/env python3
"""Concorde site favicon — the family delta wing on a dark tile.

NOT the ConcordeVPN app icon: that one carries a padlock, which is wrong
for a site that fronts the whole family. This is the five-line wing from
index.html, same silver gradient, drawn 4x and downsampled because PIL
draws without antialiasing.

The dark tile is deliberate. A bare silver wing on transparency vanishes
against a light tab bar; the tile keeps it readable in either theme and
matches the app icons, which are also dark tiles.
"""
import math
import os
import sys

from PIL import Image, ImageDraw

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
X = 4                      # supersample
S = 512 * X                # master canvas

BG = (16, 16, 19)          # --bg #101013
# wgrad, bottom-left -> top-right
STOPS = [(0.00, (0x78, 0x7e, 0x89)),
         (0.55, (0xb7, 0xbc, 0xc6)),
         (1.00, (0xf4, 0xf5, 0xf8))]

# viewBox "2 2.3 19.6 16.4" straight from the site's inline SVG
VB = (2.0, 2.3, 19.6, 16.4)
LINES = [(3.2, 17.5, 20.4, 3.5),
         (7.5, 17.5, 20.4, 7.0),
         (11.8, 17.5, 20.4, 10.5),
         (16.1, 17.5, 20.4, 14.0),
         (19.3, 17.5, 20.4, 16.6)]
STROKE = 2.4

PAD = 0.17                 # margin inside the tile, as a fraction of side
RADIUS = 0.225             # corner radius, fraction of side (Apple-ish squircle)


def grad_rgb(t):
    t = max(0.0, min(1.0, t))
    for i in range(len(STOPS) - 1):
        t0, c0 = STOPS[i]
        t1, c1 = STOPS[i + 1]
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return tuple(int(c0[j] + (c1[j] - c0[j]) * f) for j in range(3))
    return STOPS[-1][1]


def gradient_image(size):
    """Diagonal ramp: dark steel at bottom-left, bright silver at top-right."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            # project onto the (1,-1) axis, normalised to 0..1
            t = (x / size + (1.0 - y / size)) / 2.0
            px[x, y] = grad_rgb(t)
    return img


def wing_mask(size, lines=LINES, stroke=STROKE, pad=PAD):
    """The capsules, white on black, at `size`."""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    vx, vy, vw, vh = VB
    inner = size * (1 - 2 * pad)
    # preserve aspect; centre the viewBox in the inner square
    k = inner / max(vw, vh)
    ox = (size - vw * k) / 2.0
    oy = (size - vh * k) / 2.0

    def P(px_, py_):
        return (ox + (px_ - vx) * k, oy + (py_ - vy) * k)

    w = stroke * k
    for (x1, y1, x2, y2) in lines:
        a, b = P(x1, y1), P(x2, y2)
        d.line([a, b], fill=255, width=int(round(w)))
        for (cx, cy) in (a, b):          # round caps
            d.ellipse([cx - w / 2, cy - w / 2, cx + w / 2, cy + w / 2], fill=255)
    return m


def tile_mask(size):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * RADIUS), fill=255)
    return m


def master(lines=LINES, stroke=STROKE, pad=PAD, stops=STOPS):
    global STOPS
    keep = STOPS
    STOPS = stops
    try:
        tile = Image.new("RGB", (S, S), BG)
        tile.paste(gradient_image(S), (0, 0),
                   wing_mask(S, lines=lines, stroke=stroke, pad=pad))
        out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        out.paste(tile, (0, 0), tile_mask(S))
        return out
    finally:
        STOPS = keep


# Optical sizing for the .ico. At 16px the five capsules blob into a
# triangle and the dark end of the ramp sinks into the tile, so the small
# master drops the vestigial fifth line (leaving four bars, exactly like
# the app icon), thins the stroke to open the gaps, lifts the darkest stop,
# and trims the margin.
SMALL_LINES = LINES[:4]
SMALL_STOPS = [(0.00, (0x9a, 0xa0, 0xac)),
               (0.55, (0xcf, 0xd3, 0xda)),
               (1.00, (0xf8, 0xf9, 0xfb))]


def main():
    m = master()
    os.makedirs(OUT, exist_ok=True)

    small = master(lines=SMALL_LINES, stroke=2.0, pad=0.14, stops=SMALL_STOPS)

    def png(size, name):
        m.resize((size, size), Image.LANCZOS).save(os.path.join(OUT, name))
        print("  %-24s %dx%d" % (name, size, size))

    png(180, "apple-touch-icon.png")
    png(512, "icon-512.png")
    # .ico carries the small sizes browsers actually pick from. Save from a
    # LARGE source: PIL only ever downscales for `sizes`, so handing it a
    # 16px image silently yields a single-frame 16px ico.
    small.resize((256, 256), Image.LANCZOS).save(
        os.path.join(OUT, "favicon.ico"),
        sizes=[(16, 16), (32, 32), (48, 48)])
    print("  %-24s 16/32/48" % "favicon.ico")


if __name__ == "__main__":
    main()
