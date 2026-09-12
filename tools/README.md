# tools — how the generated imagery is made

Nothing in here is served (see `.assetsignore`). Everything the site shows
that isn't text is generated from source kept here, so a rebrand is a re-run
rather than an archaeology dig through old PNGs.

| Output | Made by |
|---|---|
| `favicon.ico`, `apple-touch-icon.png` | `../tools-make-favicon.py` |
| `og.png` | `../tools-make-og.py` |
| `assets/shot-ai.*`, `assets/shot-vpn.*` | `windows/*.html`, captured (below) |

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

    CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    "$CHROME" --headless=new --disable-gpu --hide-scrollbars \
      --force-device-scale-factor=2 --default-background-color=00000000 \
      --virtual-time-budget=9000 \
      --screenshot=ai.png --window-size=1280,820 "file://$PWD/windows/ai-window.html"

    "$CHROME" --headless=new --disable-gpu --hide-scrollbars \
      --force-device-scale-factor=2 --default-background-color=00000000 \
      --virtual-time-budget=9000 \
      --screenshot=vpn.png --window-size=560,900 "file://$PWD/windows/vpn-window.html"

Then trim the transparent margin (keeping the shadow), resize to 1100px and
420px wide respectively, and save both `.webp` (quality 88) and `.png` into
`assets/`.

**`--virtual-time-budget` is not optional.** Without it Chrome captures before
the Google Fonts webfonts arrive and silently falls back to system faces — the
render looks subtly wrong and nothing warns you.

`--default-background-color=00000000` is what keeps the page transparent, which
is what lets the site's starfield show around the window instead of a flat box.

## Typography, if you restage them

Michroma (`--disp`) is the display face — the wide squared one. It carries the
headline, the status word, the big figures and the lockups, always uppercase
and tracked. It ships one weight, so heavier means `-webkit-text-stroke`, and
it runs ~1.1x the width per character, so converting anything to it means
dropping the size and re-checking the fit. Never set a sentence in it: running
copy stays Space Grotesk, small labels stay IBM Plex Mono.
