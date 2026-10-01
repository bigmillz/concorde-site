#!/usr/bin/env python3
"""The ConcordeAI window's backdrop: tools/windows/backdrop-clouds.jpg.

The real app plays Apple TV aerial clips behind its window (Settings >
Enable visual effects). Those clips are Apple's and each Mac fetches them
from Apple; they are not ours to put on a public site. This draws a still
in the same spirit instead: an aerial view over clouds at dusk, cool blue-
grey shade, warm light on the cloud tops, made only from noise (fractal
Brownian motion, domain-warped, lit from the lower left) — nothing is
copied from anywhere.

    python3 tools/make-backdrop.py            # rewrites tools/windows/backdrop-clouds.jpg

Sized for the window mock (1000x1060 CSS px at 2x). To use a different still
(a photo you have the rights to), save it over that file: the mock just
covers the window with it.
"""
import os
import numpy as np
from PIL import Image

W, H = 2000, 2120
SEED = 1969
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "windows", "backdrop-clouds.jpg")


def fbm(rng, w, h, octaves, base, persistence=0.55, stretch=1.0):
    """Fractal noise in 0..1: each octave is random values on a coarse grid,
    smoothly upsampled, summed with falling amplitude."""
    total = np.zeros((h, w), np.float32)
    amp, norm = 1.0, 0.0
    for o in range(octaves):
        gw = max(2, int(base * (2 ** o) * stretch)) + 1
        gh = max(2, int(base * (2 ** o))) + 1
        g = rng.random((gh, gw)).astype(np.float32)
        img = Image.fromarray((g * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC)
        total += amp * (np.asarray(img, np.float32) / 255.0)
        norm += amp
        amp *= persistence
    return total / norm


def smoothstep(a, b, x):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def blur(a, radius):
    """Gaussian blur of a 0..1 float array (through 8 bits, plenty for light)."""
    from PIL import ImageFilter
    im = Image.fromarray(np.clip(a * 255, 0, 255).astype(np.uint8))
    return np.asarray(im.filter(ImageFilter.GaussianBlur(radius)), np.float32) / 255.0


def main():
    rng = np.random.default_rng(SEED)
    warp_x = fbm(rng, W, H, 3, 2)
    warp_y = fbm(rng, W, H, 3, 2)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    # big soft banks of cloud, a little wider than tall, domain-warped so they billow
    base = fbm(rng, W, H, 6, 2, 0.5, stretch=1.5)
    shift = 110.0
    xs = np.clip(xx + (warp_x - 0.5) * 2 * shift, 0, W - 1).astype(int)
    ys = np.clip(yy + (warp_y - 0.5) * 2 * shift, 0, H - 1).astype(int)
    dens = smoothstep(0.30, 0.66, base[ys, xs])
    puff = fbm(rng, W, H, 6, 9, 0.58)                       # cauliflower texture, kept gentle
    dens = np.clip(dens * (0.70 + 0.62 * puff), 0, 1)
    dens = blur(dens, 2)

    # soft light from the lower left: where a cloud rises toward it, it is lit
    soft = blur(dens, 9)
    toward = np.roll(np.roll(soft, 14, axis=0), -16, axis=1)
    lit = np.clip((soft - toward) * 4.2 + 0.46, 0, 1)
    lit = lit * smoothstep(0.05, 0.45, dens)

    bloom = np.exp(-(((xx - 0.20 * W) / (0.55 * W)) ** 2 + ((yy - 0.68 * H) / (0.38 * H)) ** 2))

    t = (yy / H)[..., None]
    sky = np.array([54, 72, 96], np.float32) * (1 - t) + np.array([96, 112, 128], np.float32) * t

    body = np.array([142, 150, 160], np.float32)            # cloud in diffuse light: pale blue-grey
    shade = np.array([84, 96, 112], np.float32)
    warm = np.array([255, 214, 150], np.float32)
    thick = (dens ** 1.4)[..., None]
    cloud = shade * (1 - thick) + body * thick
    cloud = cloud * (1 - lit[..., None] * 0.75 * (0.3 + 0.9 * bloom[..., None])) \
        + warm * lit[..., None] * 0.75 * (0.3 + 0.9 * bloom[..., None])

    img = sky * (1 - dens[..., None]) + cloud * dens[..., None]
    img = img + np.array([80, 46, 10], np.float32) * (bloom[..., None] * 0.30)
    img = img * (0.62 + 0.12 * bloom[..., None])            # the app dims its backdrop
    vig = 1 - 0.30 * (((xx / W - 0.5) * 1.5) ** 2 + ((yy / H - 0.5) * 1.1) ** 2)
    img = img * np.clip(vig, 0.62, 1)[..., None]
    grain = rng.normal(0, 1.3, (H, W, 1)).astype(np.float32)
    out = np.clip(img + grain, 0, 255).astype(np.uint8)
    Image.fromarray(out).save(OUT, quality=88, optimize=True, progressive=True)
    print("wrote %s (%dx%d, %d KB)" % (OUT, W, H, os.path.getsize(OUT) // 1024))


if __name__ == "__main__":
    main()
