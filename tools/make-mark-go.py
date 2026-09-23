#!/usr/bin/env python3
"""The ConcordeGo HDR nameplate, assets/mark-go-pq.mp4 and mark-go-hlg.webm,
made to match assets/mark-ai-* and assets/mark-vpn-*.

The page shows the three nameplates all or none (index.html), so ConcordeGo
needs one that reads as the same family. There is no generator for the AI
and VPN ones in this repo, so this one works from the videos themselves.
What they are, measured:
  - 2x rasters, 46px tall, grey only (U = V = 512), 72 frames at 24 fps.
  - The same code values in both files: the .webm is the .mp4's picture with
    an HLG tag instead of a PQ one.
  - A resting plate (every pixel's minimum over the loop), and every glyph
    flaring to one full-bright plate (every pixel's maximum) in turn, left to
    right. When a glyph lights depends on where its centre sits in the plate
    (u = x / width): the AI and VPN plates light glyphs at the same u at the
    same frame.
      frame = rest + h_glyph(t) * (full - rest)
    reproduces both videos to a mean error of 0.008 (PQ code, 0..1).
So ConcordeGo's plate is built from those parts:
  - CONCORDE: the AI plate's own rest and full pixels (the first eight glyphs
    are identical in AI and VPN).
  - GO: Michroma at 30px with a 0.75px stroke (the AI/VPN suffix glyphs'
    4-5px stems), the suffix's spacing, and rest/full levels learned from
    the AI and VPN suffix glyphs by depth from the glyph edge (and, for the
    rim rows, by row).
  - h_glyph(t): the AI and VPN glyph pulses, interpolated at each GO glyph's
    centre u.
Encoded with the x265 settings read out of mark-ai-pq.mp4 (crf 14, medium,
hdr10, max-cll 1000,400) and VP9 profile 2 for the HLG copy.

    python3 tools/make-mark-go.py                 # reads and writes assets/
    python3 tools/make-mark-go.py <src-dir> <out-dir>

Needs numpy, Pillow, ffmpeg built with libx265 and libvpx, and Michroma
(FONT below; the same file tools-make-og.py uses).
"""
import subprocess, sys, tempfile, os
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ASSETS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets'))
SRC, OUT = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else (ASSETS, ASSETS)
FONT = os.path.expanduser('~/Code/concorde-vpn/fonts/Michroma-Regular.ttf')
if not os.path.exists(FONT):
    sys.exit('make-mark-go: Michroma not found at %s' % FONT)
H, N, FPS = 46, 72, 24
SPLIT = 289          # first column after CONCORDE's E (ink ends at 285)
SUFFIX_X = 292       # where the AI and VPN suffixes' first glyph starts


def decode(path, w):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-f', 'rawvideo',
                          '-pix_fmt', 'yuv444p16le', '-'], capture_output=True, check=True).stdout
    a = np.frombuffer(raw, '<u2').reshape(N, 3, H, w).astype(np.float64) / 64.0
    return (a[:, 0] - 64) / 876          # PQ code, 0..1 (a little over at peaks)


def runs(cols):
    r, s = [], None
    for i, v in enumerate(cols):
        if v and s is None: s = i
        if not v and s is not None: r.append((s, i - 1)); s = None
    if s is not None: r.append((s, len(cols) - 1))
    return r


def depth(mask):
    """1 on a glyph's edge pixels (8-neighbourhood), 2 on the next ring, ..."""
    d = np.zeros(mask.shape, int); cur = mask.copy(); k = 0
    while cur.any():
        k += 1
        p = np.pad(cur, 1); er = cur.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                er &= p[1 + dy:1 + dy + cur.shape[0], 1 + dx:1 + dx + cur.shape[1]]
        d[cur & ~er] = k; cur = er
    return d


src = {}
for k, w in (('ai', 350), ('vpn', 410)):
    E = decode(f'{SRC}/mark-{k}-pq.mp4', w)
    rest, full = E.min(0), E.max(0)
    src[k] = dict(E=E, rest=rest, full=full, w=w, glyphs=runs(full.max(0) > 0.05))

# --- glyph pulses, by centre u -------------------------------------------
U, P = [], []
for k, s in src.items():
    rng = s['full'] - s['rest']
    for a, b in s['glyphs']:
        sl = slice(max(a - 1, 0), b + 2); m = rng[:, sl] > 0.15
        h = [np.median(((s['E'][t][:, sl] - s['rest'][:, sl]) / np.maximum(rng[:, sl], 1e-6))[m]) for t in range(N)]
        U.append(((a + b) / 2 + 0.5) / s['w']); P.append(np.clip(h, 0, 1))
order = np.argsort(U); U = np.array(U)[order]; P = np.array(P)[order]

# --- suffix levels by (row, depth), from the AI and VPN suffix glyphs ----
lv_rest, lv_full = {}, {}
for s in src.values():
    reg = np.zeros(s['full'].shape, bool); reg[:, SPLIT:] = True
    inside = reg & (s['full'] > 0.5)
    d = depth(inside)
    for y, x in zip(*np.nonzero(inside)):
        lv_rest.setdefault((y, min(d[y, x], 3)), []).append(s['rest'][y, x] / s['full'][y, x])
        lv_full.setdefault((y, min(d[y, x], 3)), []).append(s['full'][y, x])
def level(table, y, dd, fallback):
    for key in ((y, dd), (y, min(dd, 2)), (y, 1)):
        if key in table and len(table[key]) >= 3: return float(np.median(table[key]))
    vals = [v for (yy, d2), vs in table.items() if d2 == dd for v in vs]
    return float(np.median(vals)) if vals else fallback

# --- GO's suffix glyphs: coverage at 2x ------------------------------------
# Drawn at 4x the plate (8x CSS) with a 3px stroke, then box-filtered down:
# 0.75px a side at plate scale, which gives the 4-5px stems the AI and VPN
# suffix glyphs have (PIL's smallest stroke at plate scale, 1px a side, is
# visibly heavier).
S4 = 4
font = ImageFont.truetype(FONT, 30 * S4)
asc = font.getmetrics()[0]
def draw(text_xy, baseline, w=60):
    img = Image.new('L', (w * S4, H * S4), 0); dr = ImageDraw.Draw(img)
    for ch, x in text_xy:
        dr.text((x * S4, baseline * S4 - asc), ch, font=font, fill=255, stroke_width=3, stroke_fill=255)
    return np.asarray(img.resize((w, H), Image.BOX)).astype(float) / 255
def glyph(ch, baseline):
    c = draw([(ch, 10)], baseline)
    cols = np.nonzero(c.max(0) > 0.05)[0]
    return c[:, cols[0]:cols[-1] + 1]

# baseline: the one that puts a rendered suffix glyph on the source's rows
ai = src['ai']; a, b = ai['glyphs'][-2]                        # AI's "A"
target = (ai['full'][:, a:b + 1] > 0.5).mean(1)                 # ink per row
best = min(np.arange(38, 42.01, 0.25),
           key=lambda bl: np.abs((glyph('A', bl) > 0.5).mean(1) - target).sum())
G, O = glyph('G', best), glyph('O', best)
# the suffixes' spacing: the pair's own gap, plus the 2-3px the AI/VPN
# suffix glyphs sit wider than a plain render
pr = runs(draw([('G', 10), ('O', 10 + font.getlength('G') / S4 + 4.5)], best, 140).max(0) > 0.05)
gap = (pr[1][0] - pr[0][1] - 1) + 2.5
gx = SUFFIX_X; ox = int(round(gx + G.shape[1] + gap))
W = ox + O.shape[1] + 14; W += W % 2

rest = np.zeros((H, W)); full = np.zeros((H, W))
rest[:, :SPLIT] = ai['rest'][:, :SPLIT]; full[:, :SPLIT] = ai['full'][:, :SPLIT]
cov = np.zeros((H, W)); cov[:, gx:gx + G.shape[1]] = G; cov[:, ox:ox + O.shape[1]] = O
inside = cov > 0.5; d = depth(inside)
# Rest level = full level x a ratio. By depth from the glyph edge the source
# suffixes give 0.76 (edge ring), 0.70, 0.66 (core); the rows in between
# carry the source glyphs' own bars and bowls, so only the rim rows (the
# glyph's top two and bottom two) take their per-row values.
ink_rows = np.nonzero(inside.any(1))[0]
rim = set(ink_rows[:2]) | set(ink_rows[-2:])
ring = {1: np.median([v for (y, dd), vs in lv_rest.items() if dd == 1 and y not in rim for v in vs]),
        2: np.median([v for (y, dd), vs in lv_rest.items() if dd == 2 and y not in rim for v in vs]),
        3: np.median([v for (y, dd), vs in lv_rest.items() if dd == 3 for v in vs] or [0.66])}
fr_med = float(np.median([v for vs in lv_full.values() for v in vs]))
for y in range(H):
    for x in range(SPLIT, W):
        c = cov[y, x]
        if c <= 0.02: continue
        if inside[y, x]:
            dd = min(d[y, x], 3)
            r = level(lv_rest, y, dd, ring[dd]) if y in rim else ring[dd]
            full[y, x] = fr_med; rest[y, x] = fr_med * r
        else:                                            # anti-aliased edge
            full[y, x] = c * fr_med; rest[y, x] = full[y, x] * 0.63

# --- frames ------------------------------------------------------------------
gl = runs(full.max(0) > 0.05)
owner = np.zeros(W, int)
centres = np.array([(a + b) / 2 for a, b in gl])
for x in range(W): owner[x] = int(np.argmin(np.abs(centres - x)))
pulse = np.array([[np.interp((cx + 0.5) / W, U, P[:, t]) for t in range(N)] for cx in centres])
frames = np.empty((N, H, W))
for t in range(N):
    frames[t] = rest + pulse[owner, t][None, :] * (full - rest)

Y = np.clip(np.round(64 + 876 * frames), 64, 1019).astype('<u2')
C = np.full((N, H // 2, W // 2), 512, '<u2')
with tempfile.NamedTemporaryFile(suffix='.yuv', delete=False) as f:
    for t in range(N):
        f.write(Y[t].tobytes()); f.write(C[t].tobytes()); f.write(C[t].tobytes())
    raw = f.name
common = ['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'yuv420p10le', '-s', f'{W}x{H}',
          '-r', str(FPS), '-i', raw, '-color_primaries', 'bt2020', '-colorspace', 'bt2020nc', '-color_range', 'tv']
tag = lambda trc: ['-vf', f'setparams=range=tv:color_primaries=bt2020:color_trc={trc}:colorspace=bt2020nc']
subprocess.run(common + tag('smpte2084') + ['-c:v', 'libx265', '-preset', 'medium', '-tag:v', 'hvc1', '-color_trc', 'smpte2084',
    '-x265-params', 'crf=14:hdr10=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:'
                    'colormatrix=bt2020nc:range=limited:max-cll=1000,400:log-level=error',
    f'{OUT}/mark-go-pq.mp4'], check=True)
subprocess.run(common + tag('arib-std-b67') + ['-c:v', 'libvpx-vp9', '-profile:v', '2', '-pix_fmt', 'yuv420p10le', '-crf', '14',
    '-b:v', '0', '-color_trc', 'arib-std-b67', f'{OUT}/mark-go-hlg.webm'], check=True)
os.unlink(raw)
print(f'mark-go: {W}x{H}, baseline {best}, G at {gx}, O at {ox}, glyphs {len(gl)}; '
      f'rest/full medians {np.median(rest[full > 0.5]):.3f}/{np.median(full[full > 0.5]):.3f}')
