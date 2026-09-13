#!/usr/bin/env python3
"""Concorde site favicon — the family delta wing on a dark tile.

NOT the ConcordeVPN app icon: that one carries a padlock, which is wrong for
a site fronting the whole family.

OPTICAL SIZING IS THE WHOLE POINT HERE. A single master downsampled to 16px
turns into a dark square with a grey smudge in it — the bars collapse into
each other and the dark end of the silver ramp sinks into the tile, so on a
dark browser tab the icon reads as "missing image". Each .ico frame is
therefore drawn from its OWN master: fewer bars, thicker strokes, brighter
ramp and less padding as the canvas shrinks.

PIL cannot do this on its own — `Image.save(sizes=[...])` downsamples one
source image — so the .ico is assembled by hand below. PNG-compressed frames
inside an .ico are understood by every browser that matters.
"""
import io
import math
import os
import struct
import sys

from PIL import Image, ImageDraw

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
X = 8                      # supersample factor

BG = (16, 16, 19)          # --bg #101013

# wgrad, bottom-left -> top-right
STOPS_TRUE = [(0.00, (0x78, 0x7e, 0x89)),
              (0.55, (0xb7, 0xbc, 0xc6)),
              (1.00, (0xf4, 0xf5, 0xf8))]
# Lifted ramp for small frames: the true ramp's dark end is invisible on a
# tile this size.
STOPS_LIFT = [(0.00, (0xc2, 0xc6, 0xce)),
              (0.55, (0xe4, 0xe6, 0xea)),
              (1.00, (0xfb, 0xfc, 0xfd))]

VB = (2.0, 2.3, 19.6, 16.4)
LINES = [(3.2, 17.5, 20.4, 3.5),
         (7.5, 17.5, 20.4, 7.0),
         (11.8, 17.5, 20.4, 10.5),
         (16.1, 17.5, 20.4, 14.0),
         (19.3, 17.5, 20.4, 16.6)]
RADIUS = 0.225             # corner radius as a fraction of the side

# size -> (how many bars, stroke in viewBox units, padding, ramp)
# Smaller canvas: fewer bars so each one gets real pixels, fatter strokes so
# they survive, brighter ramp so the mark reads against a dark tab.
PLAN = {
    # 16px: bracketed against real tab grounds. Fatter than this and the
    # three bars merge into a solid wedge; thinner and they dissolve.
    16:  (3, 2.1, 0.060, STOPS_LIFT),
    32:  (4, 2.5, 0.085, STOPS_LIFT),
    48:  (4, 2.4, 0.105, STOPS_TRUE),
    180: (5, 2.4, 0.170, STOPS_TRUE),
}


def grad_rgb(t, stops):
    t = max(0.0, min(1.0, t))
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return tuple(int(c0[j] + (c1[j] - c0[j]) * f) for j in range(3))
    return stops[-1][1]


def gradient_image(size, stops):
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            px[x, y] = grad_rgb((x / size + (1.0 - y / size)) / 2.0, stops)
    return img


def wing_mask(size, lines, stroke, pad):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    vx, vy, vw, vh = VB
    k = size * (1 - 2 * pad) / max(vw, vh)
    ox = (size - vw * k) / 2.0
    oy = (size - vh * k) / 2.0

    def P(a, b):
        return (ox + (a - vx) * k, oy + (b - vy) * k)

    w = stroke * k
    for (x1, y1, x2, y2) in lines:
        a, b = P(x1, y1), P(x2, y2)
        d.line([a, b], fill=255, width=max(1, int(round(w))))
        for (cx, cy) in (a, b):
            d.ellipse([cx - w / 2, cy - w / 2, cx + w / 2, cy + w / 2], fill=255)
    return m


def tile_mask(size):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * RADIUS), fill=255)
    return m


def frame(target):
    """One icon at `target` px, drawn big from its own plan and downsampled."""
    bars, stroke, pad, stops = PLAN[target]
    S = target * X
    tile = Image.new("RGB", (S, S), BG)
    tile.paste(gradient_image(S, stops), (0, 0),
               wing_mask(S, LINES[:bars], stroke, pad))
    big = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    big.paste(tile, (0, 0), tile_mask(S))
    return big.resize((target, target), Image.LANCZOS)


def write_ico(path, images):
    """Hand-rolled .ico with PNG-compressed frames, one per size."""
    images = sorted(images, key=lambda im: im.size[0])
    blobs = []
    for im in images:
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        blobs.append(buf.getvalue())
    header = struct.pack("<HHH", 0, 1, len(images))     # reserved, type=icon, count
    offset = 6 + 16 * len(images)
    entries, body = b"", b""
    for im, blob in zip(images, blobs):
        w, h = im.size
        entries += struct.pack("<BBBBHHII",
                               0 if w >= 256 else w,    # 0 means 256
                               0 if h >= 256 else h,
                               0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        body += blob
    with open(path, "wb") as f:
        f.write(header + entries + body)


def main():
    os.makedirs(OUT, exist_ok=True)
    ico_sizes = [16, 32, 48]
    write_ico(os.path.join(OUT, "favicon.ico"), [frame(s) for s in ico_sizes])
    print("  %-24s %s" % ("favicon.ico", "/".join(str(s) for s in ico_sizes)))
    frame(180).save(os.path.join(OUT, "apple-touch-icon.png"))
    print("  %-24s 180x180" % "apple-touch-icon.png")
    # Loose PNG frames too: some browsers prefer an explicit sizes= PNG over
    # picking a frame out of the .ico, and they are the same pixels anyway.
    for s in (16, 32):
        name = "favicon-%d.png" % s
        frame(s).save(os.path.join(OUT, name))
        print("  %-24s %dx%d" % (name, s, s))


if __name__ == "__main__":
    main()
