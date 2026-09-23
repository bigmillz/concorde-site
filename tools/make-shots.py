#!/usr/bin/env python3
"""Capture the product windows and normalise them to one geometry.

Each product card on the site shows its app's window beside the card's
text, and a script on the page sizes each window to that text. For the
page to place and centre a window it has to know where the window is
inside its image, so every asset agrees on one geometry: the opaque window
occupies exactly rows MARGIN..MARGIN+WIN_H and is surrounded by the same
MARGIN of transparent shadow room on all four sides. The page knows that
one number (SHADOW in index.html's fit script and in its Enlarge dialog)
and never has to measure an image.

The geometry is exact by construction, not by measurement: the window is
located in the raw 2x capture (where it is pixel-aligned), and ONE resize
maps window+margin straight onto the output canvas, so the window's edges
land on pixel boundaries with the same phase in every image.

The box-shadow reaches further than MARGIN. Rather than crop it to a hard
line (visible as a faint step on the page), the outer FEATHER px of the
margin fade the alpha to zero, so the shadow ends in nothing.

The captures are 2x (Retina) and the outputs stay 2x: WIN_H is twice the
tallest the site draws a window (about 650 CSS px, the desktop cards), and
MARGIN and FEATHER keep the proportions the first, shorter windows had
(80 and 30 on a 900px window).

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

WIN_H = 1300     # window height in the finished asset (px)
MARGIN = 116     # transparent room around the window, every side (px).
                 # index.html knows this number (SHADOW, in the fit
                 # script and the Enlarge dialog) and the ratio
                 # (WIN_H + 2 * MARGIN) / WIN_H (1.18, in the stacked
                 # CSS): change one, change the others.
FEATHER = 43     # outer px of the margin over which the shadow fades out
OPAQUE = 250     # alpha at or above this is window; below is shadow or air

# name -> (source page, capture window in CSS px). The capture must hold
# the window plus MARGIN/k of shadow on every side, where k = WIN_H / the
# window's 2x pixel height; the script refuses a capture that is too small.
SOURCES = {
    "ai":  ("ai-window.html",  (1400, 1520)),
    "vpn": ("vpn-window.html", (780, 1220)),
    "go":  ("go-window.html",  (1470, 1600)),   # a browser frame round go-capture.png
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
    print("  window rows %d..%d in every image; update the <img width height> in index.html if the sizes changed"
          % (MARGIN, MARGIN + WIN_H))


if __name__ == "__main__":
    main()
