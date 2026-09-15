# tools — how the generated imagery is made

Nothing in here is served (see `.assetsignore`). Everything the site shows
that isn't text is generated from source kept here, so a rebrand is a re-run
rather than an archaeology dig through old PNGs.

| Output | Made by |
|---|---|
| `favicon.ico`, `favicon-16.png`, `favicon-32.png`, `apple-touch-icon.png` | `../tools-make-favicon.py` |
| `og.png` | `../tools-make-og.py` |
| the download blocks in `index.html` | `sync-releases.py` from GitHub Releases |
| `assets/shot-ai.*`, `assets/shot-vpn.*` | `make-shots.py` from `windows/*.html` (below) |

## The download blocks

    python3 tools/sync-releases.py            # rewrite from GitHub Releases
    python3 tools/sync-releases.py --check    # exit 1 if the page is behind

Everything between `<!-- releases:ai -->` / `<!-- releases:vpn -->` and
their closing markers in `index.html` is generated; hand edits there are
overwritten on the next run. Stable is the newest non-prerelease; an RC or
beta shows as a second channel only while it is newer than stable. Versions
are read from the asset filenames (release names have drifted from the
files before — the 6.0 candidates shipped as `ConcordeAI-6.1.0.*`), dates
from the publish time in UTC. Each channel gets a "Release notes" dropdown:
the bullets in `release-notes.json` for that tag when we have written them
(keep them to what a visitor would notice — nobody needs the two-pixel
starfield tweak), otherwise an automatic condensation of the GitHub body.

The VPN repo is private, so its buttons point at the public mirror
`bigmillz/concordevpn-releases`; a release that is not mirrored never reaches
the page.

## The product windows

`windows/ai-window.html` and `windows/vpn-window.html` are idealised app
windows — the real UIs, staged to show what each product is *for*, rather
than screenshots of whatever happened to be on screen. They are HTML so they
stay crisp at any size and can be restaged without re-shooting.

Two rules they exist to enforce:

1. **No IP address, ever.** A real screenshot of ConcordeVPN shows the live
   exit IP. Publishing that puts an exit node on a marketing page, which is
   what gets an address scanned and reputation-listed. The generated window
   omits the concept entirely — not a placeholder, absent.
2. **No real user data.** The conversation titles in the AI window are
   invented and work-shaped. A real capture lists whatever you were actually
   asking about.

### Regenerating

    python3 tools/make-shots.py

That captures both windows with headless Chrome at 2x, then normalises them
into a **matched pair**: in each output the opaque window occupies exactly
the same rows (80–980) inside the same 80px transparent margin on all four
sides — placed by one resize from the pixel-aligned capture, so the edges
land on pixel boundaries with the same phase in both. The site gives both
images one CSS height, so identical geometry is what makes the two windows
land on the same top and bottom line to the pixel — if you ever crop these
by hand, that alignment is the first thing to break. The box-shadow reaches
further than the margin; the outer 30px of the margin fade it to nothing
instead of cutting it (a cut shows on the page as a faint line). The script
refuses a capture whose shadow runs off the edge, so if it complains,
enlarge that entry's size in `SOURCES`. Both `.png` and `.webp` are written
to `assets/`; update the `width`/`height` attributes in `index.html` if the
printed dimensions change.

**`--virtual-time-budget` is not optional** (the script passes it). Without
it Chrome captures before the Google Fonts webfonts arrive and silently falls
back to system faces — the render looks subtly wrong and nothing warns you.

`--default-background-color=00000000` is what keeps the page transparent, which
is what lets the site's starfield show around the window instead of a flat box.

### Checking a layout

    node tools/measure.mjs https://flyconcordefly.com 375x812 \
      'document.querySelector(".gal-vpn img").getBoundingClientRect().toJSON()' [shot.png]

Drives headless Chrome over the DevTools protocol at an exact viewport
(device emulation, so phone widths work) and prints what the expression
returns; it exits non-zero if the page fails to load or the expression
throws. `MEASURE_HOLD=assets/shot-` leaves requests containing that text
pending, which is what a not-yet-loaded lazy image looks like — measure with
and without it to prove a layout does not jump when the images arrive. **Do not check phone layouts with `--window-size`:** headless
Chrome will not lay out narrower than ~500px, so a `--window-size=390,…`
capture is a 500px layout cropped to 390 — it shows phantom overflow no phone
has.

## Typography, if you restage them

Michroma (`--disp`) is the display face — the wide squared one. It carries the
headline, the status word, the big figures and the lockups, always uppercase
and tracked. It ships one weight, so heavier means `-webkit-text-stroke`, and
it runs ~1.1x the width per character, so converting anything to it means
dropping the size and re-checking the fit. Never set a sentence in it: running
copy stays Space Grotesk, small labels stay IBM Plex Mono.
