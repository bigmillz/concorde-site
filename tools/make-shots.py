#!/usr/bin/env python3
"""Capture the product windows and normalise them into a matched pair.

The two windows sit side by side on the site at ONE CSS height, so the
assets must agree on geometry or nothing lines up: in each output the
opaque window is exactly WIN_H tall and is surrounded by the same MARGIN of
transparent shadow room on all four sides. Two images with identical window
height and identical margins, given the same CSS height, render their
windows to the pixel at the same top and bottom — that is the whole trick,
and it is why the site never has to know how big the shadow is.

The captures are 2x (Retina) and the outputs stay 2x: WIN_H is twice the
largest height the site renders the windows at.

Run from anywhere:  python3 tools/make-shots.py
"""
import os
import subprocess
import sys
import tempfile

from PIL import Image

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.normpath(os.path.join(HERE, "..", "assets"))

WIN_H = 900      # window height in the finished asset (px)
MARGIN = 80      # transparent room around the window, every side (px); the
                 # box-shadow is soft and reaches ~78px at this scale
OPAQUE = 250     # alpha at or above this is window, below is shadow/air
SHADOW = 4       # alpha at or above this must still fit inside MARGIN

# name -> (source page, capture window in CSS px — must be larger than the
# window plus its shadow, or the shadow gets clipped)
SOURCES = {
    "ai":  ("ai-window.html",  (1280, 820)),
    "vpn": ("vpn-window.html", (560, 900)),
}


def capture(html, size, out):
    # --virtual-time-budget is what waits for the Google Fonts webfonts;
    # without it the window silently renders in a fallback face.
    subprocess.run([
        CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=2", "--default-background-color=00000000",
        "--virtual-time-budget=9000", "--screenshot=%s" % out,
        "--window-size=%d,%d" % size, "file://%s" % html,
    ], check=True, capture_output=True)


def window_box(im, threshold):
    return im.getchannel("A").point(lambda v: 255 if v >= threshold else 0).getbbox()


def normalise(path):
    im = Image.open(path).convert("RGBA")
    x0, y0, x1, y1 = window_box(im, OPAQUE)
    k = WIN_H / float(y1 - y0)
    im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)

    x0, y0, x1, y1 = window_box(im, OPAQUE)          # re-measure after resampling
    sx0, sy0, sx1, sy1 = window_box(im, SHADOW)
    reach = max(x0 - sx0, y0 - sy0, sx1 - x1, sy1 - y1)
    if reach > MARGIN:
        sys.exit("shadow reaches %dpx beyond the window; raise MARGIN above that" % reach)

    box = (x0 - MARGIN, y0 - MARGIN, x1 + MARGIN, y1 + MARGIN)
    out = im.crop(box)                                # pads with transparent if outside
    assert out.height == (y1 - y0) + 2 * MARGIN
    return out, (x1 - x0, y1 - y0), reach


def main():
    os.makedirs(ASSETS, exist_ok=True)
    heights, windows = set(), set()
    with tempfile.TemporaryDirectory() as tmp:
        for name, (html, size) in SOURCES.items():
            raw = os.path.join(tmp, name + ".png")
            capture(os.path.join(HERE, "windows", html), size, raw)
            out, (ww, wh), reach = normalise(raw)
            base = os.path.join(ASSETS, "shot-" + name)
            out.save(base + ".png", optimize=True)
            out.save(base + ".webp", quality=88, method=6)
            heights.add(out.height)
            windows.add(wh)
            print("  shot-%-4s %4dx%-4d  window %4dx%d  shadow reach %dpx  (margin %d)"
                  % (name, out.width, out.height, ww, wh, reach, MARGIN))
    if len(heights) != 1 or len(windows) != 1:
        sys.exit("outputs disagree: heights %s, windows %s" % (sorted(heights), sorted(windows)))
    # resampling can land a pixel or two under WIN_H; what matters is that
    # both agree, which the check above guarantees
    h, wh = heights.pop(), windows.pop()
    print("  both %dpx tall; window %dpx of that (%.4f)" % (h, wh, wh / float(h)))


if __name__ == "__main__":
    main()
