#!/usr/bin/env python3
"""Capture the product windows and normalise them into a matched pair.

The two windows sit side by side on the site at ONE CSS height, so the
assets must agree on geometry or nothing lines up: in each output the
opaque window occupies exactly rows MARGIN..MARGIN+WIN_H and is surrounded
by the same MARGIN of transparent shadow room on all four sides. Two images
with identical window rows, given the same CSS height, render their windows
on the same top and bottom line — that is the whole trick, and it is why
the site never has to know how big the shadow is.

The geometry is exact by construction, not by measurement: the window is
located in the raw 2x capture (where it is pixel-aligned), and ONE resize
maps window+margin straight onto the output canvas, so the window's edges
land on pixel boundaries in both images with the same phase.

The box-shadow reaches further than MARGIN. Rather than crop it to a hard
line (visible as a faint step on the page), the outer FEATHER px of the
margin fade the alpha to zero, so the shadow ends in nothing.

The captures are 2x (Retina) and the outputs stay 2x: WIN_H is twice the
largest height the site renders the windows at.

Run from anywhere:  python3 tools/make-shots.py
"""
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops, ImageDraw

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.normpath(os.path.join(HERE, "..", "assets"))

WIN_H = 900      # window height in the finished asset (px)
MARGIN = 80      # transparent room around the window, every side (px). The
                 # two images abut on the page, so this is also half the
                 # visible gap between the windows.
FEATHER = 30     # outer px of the margin over which the shadow fades out
OPAQUE = 250     # alpha at or above this is window; below is shadow or air

# name -> (source page, capture window in CSS px). The capture must hold
# the window plus MARGIN/k of shadow on every side, where k = WIN_H / the
# window's 2x pixel height; the script refuses a capture that is too small.
SOURCES = {
    "ai":  ("ai-window.html",  (1520, 1120)),
    "vpn": ("vpn-window.html", (780, 1220)),
}


def capture(html, size, out):
    # --virtual-time-budget is what waits for the Google Fonts webfonts;
    # without it the window silently renders in a fallback face.
    r = subprocess.run([
        CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=2", "--default-background-color=00000000",
        "--virtual-time-budget=9000", "--screenshot=%s" % out,
        "--window-size=%d,%d" % size, "file://%s" % html,
    ], capture_output=True, text=True)
    if r.returncode:
        sys.exit("chrome exited %d capturing %s:\n%s" % (r.returncode, html, r.stderr.strip()[-2000:]))


def alpha_box(im, threshold):
    return im.getchannel("A").point(lambda v: 255 if v >= threshold else 0).getbbox()


def normalise(path):
    raw = Image.open(path).convert("RGBA")
    sx0, sy0, sx1, sy1 = alpha_box(raw, 1)
    if sx0 == 0 or sy0 == 0 or sx1 == raw.width or sy1 == raw.height:
        sys.exit("%s: the shadow runs off the capture; enlarge its SOURCES size" % path)

    x0, y0, x1, y1 = alpha_box(raw, OPAQUE)           # the window, pixel-exact at 2x
    k = WIN_H / float(y1 - y0)
    m = MARGIN / k                                     # the margin, in raw px
    box = (x0 - m, y0 - m, x1 + m, y1 + m)
    if box[0] < 0 or box[1] < 0 or box[2] > raw.width or box[3] > raw.height:
        sys.exit("%s: no room for MARGIN around the window; enlarge its SOURCES size" % path)

    out_w = int(round((x1 - x0) * k)) + 2 * MARGIN
    out = raw.resize((out_w, WIN_H + 2 * MARGIN), Image.LANCZOS, box=box)

    fade = Image.new("L", out.size, 255)
    d = ImageDraw.Draw(fade)
    for i in range(FEATHER):
        t = (i + 0.5) / FEATHER
        d.rectangle((i, i, out.width - 1 - i, out.height - 1 - i),
                    outline=int(round(255 * t * t * (3 - 2 * t))))
    out.putalpha(ImageChops.multiply(out.getchannel("A"), fade))

    # how far the faint (alpha >= 4) shadow reached in the capture, at output
    # scale — informational; whatever lies beyond MARGIN is what the feather
    # is quietly discarding
    fx0, fy0, fx1, fy1 = alpha_box(raw, 4)
    reach = int(round(max(x0 - fx0, y0 - fy0, fx1 - x1, fy1 - y1) * k))
    return out, (x1 - x0, y1 - y0), reach


def main():
    os.makedirs(ASSETS, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name, (html, size) in SOURCES.items():
            raw = os.path.join(tmp, name + ".png")
            capture(os.path.join(HERE, "windows", html), size, raw)
            out, (ww, wh), reach = normalise(raw)
            base = os.path.join(ASSETS, "shot-" + name)
            out.save(base + ".png", optimize=True)
            out.save(base + ".webp", quality=88, method=6)
            print("  shot-%-4s %4dx%-4d  raw window %4dx%d  shadow reached %dpx, margin %d (fades over the last %d)"
                  % (name, out.width, out.height, ww, wh, reach, MARGIN, FEATHER))
    print("  window rows %d..%d in both; update the <img width height> in index.html if the sizes changed"
          % (MARGIN, MARGIN + WIN_H))


if __name__ == "__main__":
    main()
