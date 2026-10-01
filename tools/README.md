# tools — how the generated imagery is made

Nothing in here is served (see `.assetsignore`). Everything the site shows
that isn't text is generated from source kept here, so a rebrand is a re-run
rather than an archaeology dig through old PNGs.

| Output | Made by |
|---|---|
| `favicon.ico`, `favicon-16.png`, `favicon-32.png`, `apple-touch-icon.png` | `../tools-make-favicon.py` |
| `og.png` | `../tools-make-og.py` |
| the download blocks in `index.html` | `sync-releases.py` from GitHub Releases |
| their release notes, and ConcordeGo's | `summarize-notes.py` (Claude, in the Action) into `release-notes.auto.json`, read by `sync-releases.py` |
| `assets/shot-ai.*`, `assets/shot-vpn.*`, `assets/shot-go.*` | `make-shots.py` from `windows/*.html` (below) |
| `assets/mark-go-pq.mp4`, `assets/mark-go-hlg.webm` | `make-mark-go.py` from `assets/mark-ai-*` and `assets/mark-vpn-*` |

## The download blocks

    python3 tools/sync-releases.py            # rewrite from GitHub Releases
    python3 tools/sync-releases.py --check    # exit 1 if the page is behind

Everything between `<!-- releases:ai -->` / `<!-- releases:vpn -->` and
their closing markers in `index.html` is generated (and ConcordeGo's notes
list between `<!-- releases:go -->` markers — see
[ConcordeGo's notes](#concordegos-notes)); hand edits there are
overwritten on the next run. **Never rebase a generated change onto
another one** — two syncs that each insert the same block replay as two
insertions with no conflict, and the page ships a channel twice. Land on
the new tip and regenerate instead; that is what the Action and
`release.sh` do. The script repairs a duplicate it finds and refuses to
write a page that still has one. Stable is the newest non-prerelease; an RC or
beta shows as a second channel only while it is newer than stable; Nightly
is the one rolling release tagged `nightly` (title `1.3 nightly <commit>`),
shown with its commit and a "Built" date whenever one exists. Versions
are read from the asset filenames (release names have drifted from the
files before — the 6.0 candidates shipped as `ConcordeAI-6.1.0.*`), dates
from the publish time in UTC. Each channel gets a "Release notes" dropdown;
how those are written is the next section.

Both apps' `release.sh` run this as the last step of a cut, so a beta, RC
or stable is on the page within seconds. `.github/workflows/sync-releases.yml`
is the safety net for everything else — chiefly the nightlies, which are
built by Actions and can touch nothing here. Its cron asks for every 15
minutes; GitHub delivers it every few HOURS, so do not rely on it for
anything a person is waiting to see.

All three writers land on the current tip and REGENERATE rather than
rebasing a generated change, and each refuses to reset over a checkout
with unpushed work in it.

The VPN repo is private, so its buttons point at the public mirror
`bigmillz/concordevpn-releases`; a release that is not mirrored never reaches
the page.

## Release notes

Short and for visitors: at most five lines per channel, **new features
first, then things you can see or feel (UI, speed), then fixes a user would
have noticed**. Small fixes, polish, alignment, moved controls, refactors,
tests, review rounds and release plumbing are not listed; when there were
any, one closing line says "Plus smaller fixes and polish" and links to the
full notes on GitHub. Nobody needs the two-pixel starfield tweak.

**What a channel's notes are made from.** Every note the channel stands for,
newest first: a prerelease covers every beta/RC since the last stable, a
stable everything since the previous stable (a promoted beta's own
paraphrase is skipped), a nightly its own build — "what I'm working on right
now". For each release that is our bullets in `release-notes.json` if we
wrote some for its tag, else every bullet of its GitHub body (install
instructions dropped). **One trap:** a stable whose version already went
out as a beta is treated as a restatement of its betas, so its own body is
skipped. When a stable *adds* something the betas never had (VPN 1.3's
build, v48, added the What's new box and the update dialog), give its tag
bullets in `release-notes.json`; that brings them back.

**What the page shows** — the first of these that exists:

1. `release-notes.json` → `"lines"` → repo → version (e.g. `"1.4"`): a
   hand-written summary of a whole line. The manual override; never used for
   a nightly. Empty today.
2. `release-notes.auto.json` → repo → *key*: a summary of exactly this
   channel's notes. The key is a hash of the notes, the prompt file
   `release-notes-prompt.md` and the model (`channel_key()` in
   `sync-releases.py`), so a summary can never outlive the notes it was
   written from. Written by Claude in the Action, or by hand.
3. A keyword ranking in `sync-releases.py` (`rank_notes()`), which needs
   nothing: near-repeats merged ("Settings gains Flush the DNS cache" in
   three builds is one line), features / visible changes / speed scored up,
   polish, tests and internals scored down (`SCORES` — each rule a regex
   and a weight), at most five lines shown and the rest folded into "Plus N
   smaller fixes and polish". A body note like "**Video.** Ask for…" is
   shown in the house "**Video** — ask for…" form and cut back at a clause,
   not mid-phrase. Our own bullets that already fit are shown as written.
   This is what a release script's local sync produces, and what the page
   shows until the Action has summarized a new channel.

**Who writes the summaries.** `summarize-notes.py`, only in the GitHub Action
(it needs the `anthropic` package and the API key; the release scripts and
`sync-releases.py` stay plain `python3`). It walks the same channels the
page shows, and for any whose key is not in `release-notes.auto.json` asks
Claude (Opus 5.5) for a summary using the rules in `release-notes-prompt.md`,
then the Action regenerates the page and commits both files. Each distinct
set of notes is summarized once. The Action also runs on any push that
touches `index.html` or these files, so a release script's sync is
summarized a minute or two after it lands. A refusal or an unusable answer
is recorded as `"failed"` (the page ignores it, and it is not paid for
again for 7 days, then asked once more); a network, rate-limit or server
error is not recorded and ends that run, so the next run retries. A
refusal that happened only because the fallback model was busy is treated
the same way, not recorded. Each request times out after 90 seconds (one
retry), no new request starts after 3 minutes, and the Action kills the
summarizer at 8 and the whole job at 20, so a slow API delays the sync by
minutes, never hours; each result is written the moment it arrives, so a
kill loses nothing paid for. Only the first push attempt calls the API; a
retry after a rejected push reuses what was already bought. It calls
nothing and writes nothing while `release-notes.auto.json` does not parse,
or while `index.html` is in a state the sync would refuse to write.
`--dry-run` says what it would ask without calling anything.

**Editing a summary by hand.** Change the `"bullets"` of the entry in
`release-notes.auto.json` (its `"channel"` says which one it is), or
`"fold"` to add or drop the closing line, and push. It sticks until that
channel's notes change, which gives it a new key. To have Claude try again,
delete the entry. `**bold**` is the only markup; everything else is
escaped. **Keep the file valid JSON** (watch for a trailing comma): while
it does not parse, the page falls back to the keyword ranking and the
Action summarizes nothing and says so in its log with an error — nothing
is overwritten, so fixing the typo restores everything. To change the rules for every channel, edit
`release-notes-prompt.md`: that changes every key, so the next Action run
re-summarizes all of them. Entries no channel refers to any more are
removed by the next run.

**Set up once:** add the API key as a repository secret — GitHub →
concorde-site → Settings → Secrets and variables → Actions → New repository
secret, name `ANTHROPIC_API_KEY`. Until it exists the Action skips the
summarizer and the page shows the keyword-ranked notes (and the hand-written
summaries already in `release-notes.auto.json`, while their notes last).

Then set a monthly **spend limit** on the Anthropic Console workspace that
owns the key (Console → Settings → Limits; $20 is plenty). Nothing in the
code caps spending across runs.

**Cost.** The summarizer uses Claude Opus 5.5 at medium effort: $4 / $20 per
million input / output tokens. A request is
about 1,100–1,900 input tokens (the prompt is ~900; the VPN's 1.4 RC with 32
notes is the largest so far) and typically a few hundred to ~2,000 output
tokens including its thinking — roughly **1 to 5 cents per summary**, 2–3
typically. A summary is bought only when a channel's notes change: a new
beta, RC or stable, or a new nightly that the Action catches (it runs
every few hours and on pushes, so intermediate nightlies are skipped).
Expect a few dollars a month; a month of many nightlies, each caught by
its own run, could reach $10–30. The worst case for one call is about
$0.33 (all 16,000 output tokens used), so about $2 for a run that
re-summarizes all six channels, which is what editing the prompt file
does. Each call's tokens and cost are in the Action's log. ConcordeGo's
notes are bigger and change more often: see below.

### ConcordeGo's notes

ConcordeGo is a website with no releases, so its card is hand-written —
except the list in its "Release notes" dropdown, which `sync-releases.py`
writes between `<!-- releases:go -->` and `<!-- /releases:go -->` (those two
markers sit at twenty spaces of indent, inside the card's `sum-body`; the
script matches them exactly and refuses a page where they are missing,
doubled, outside the Go card or wrapped round anything but one list, or
where the card has a second bare `<ul>` or Release notes box beside them).

**What they are made from.** The build go.flyconcordefly.com says it is
running (the `data-build` stamp in its footer, the one `worker.js` reads;
only the first 512 KB of the page are read, 10-second timeout). **If the
site cannot be read, the list is left as it is** and the summarizer neither
pays nor prunes for Go that run: the branch tip may be ahead of what is
deployed, and the deployed build's summary is needed again as soon as the
site answers. The tip of the branch in `GO_BRANCH` in `sync-releases.py`
stands in only to fill an empty list (`<ul></ul>`) — change that one constant
if Go's source moves. If a GitHub runner is ever blocked from reading the
site for good, the Go notes stop moving (the Action log says "the live site
could not be read" each run) until a local sync on a Mac that can read it. ConcordeGo has no
repo of its own: it is `concorde-travel/` in `bigmillz/concordeai`, on an
unmerged branch. The notes are the subject lines of the commits that
touched `concorde-travel/` in the **30 days before that build's own commit
date** (not before today, so the notes and their key change only when Go
deploys), newest first, "ConcordeGo: " dropped, merges and exact repeats
skipped, at most 150 (the rest become an "...and N more" note that only
says the list is incomplete). The same `gh api` calls as the apps', so the
Action's token lifts the rate limit. **If GitHub or `gh` fails, the list is
left exactly as it is** — the sync never fails over it and never blanks it.

**What is shown**: the summary in `release-notes.auto.json` under
`"concordego"` (its own key, not the AI repo's, so pruning one never touches
the other) for exactly these notes; else a keyword fallback (`go_rank()`),
which judges each subject clause by clause — admin pages, allowlists, tests,
pins, research, tables, records, deployment, mockups, credits, data
plumbing, and anything naming a person, an email address, a domain or an
IP are dropped whole, polish and form layout score down, features score up,
British spellings are made American — shows
one line per feature however many commits built it, whole clauses only,
and closes with "Plus more changes" (no count: commits are not changes).
The closing line never links anywhere: the history is on an unmerged branch
of another repo.

**Who summarizes it**: the same Action run, the same prompt file and model.
The Go-specific instructions — it is a website that deploys continuously;
summarize the 30 days like a prerelease span; leave out admin pages,
allowlists, internal checks, tests, research notes, deployment, mockups and
data plumbing unless a visitor would see it, and that a source or check
named in a subject ("the airlines' own seat maps", "parity with Google
Flights") is where figures came from, not a feature — are in `PRODUCT` and `SPAN`
in `summarize-notes.py`, i.e. in the request, not the prompt file, so
adding Go did not re-key the six app channels. (Editing that `SPAN` text
does not change any key either; delete the `"concordego"` entry to have it
re-asked.)

**When it refreshes.** A Go deploy does not push to this repo, so the Go
notes refresh when the Action next runs: on its schedule (asked for every
15 minutes, delivered every 2–5 hours) or on any push here. Until then the
page shows the previous build's notes. A release script's local sync also
rewrites the list — with the keyword fallback when the build is newer than
the last summary — and the push it makes starts the Action, which puts the
summary back within minutes.

**Cost.** One call per Go build the Action sees (intermediate builds
between two runs are skipped). The request is ~151 commit subjects, about
16,000 characters — roughly 5,000–6,000 input tokens with the prompt, so
~2 cents in at Opus 5.5's $4 / $20, plus 1,000–4,000 output tokens with
thinking, 2–8 cents: about **4–10 cents per Go summary**, $0.34 at worst.
While Go is under heavy development (172 commits in its first nine days)
nearly every Action run sees a new build: 5–10 calls a day, roughly $8–20 a
month on top of the apps.
Quiet weeks cost nothing. Allow for that in the Console spend limit.

## ConcordeGo's version line

The Go card shows one line, "BETA 0.1.4075" (BETA in the size of the
apps' version numbers, the number small like their "beta 1"): no tabs, no old
versions. ConcordeGo is versioned 0.1.<build>, the build counting every
commit ever made, and publishes it at
`https://go.flyconcordefly.com/version` (no sign-in, no cost):

    {"product": "ConcordeGo", "version": "0.1.4075", "channel": "beta", "updated": "2026-09-27"}

`sync-releases.py` writes the version and date into the card on every sync
(the release scripts and the Action), and `worker.js` stamps them on every
request (cached five minutes, `X-Go` header). Anything missing, malformed or
unreachable leaves the last written values; until a version has ever been
read, the card shows just "BETA" and the date. If the answer carries a
`"commit"`, the Go release notes use it to find the deployed build;
otherwise they read the footer's `data-build`.

## What's new

The section above Products shows the latest update on each channel, in
order newest first: each app's newest stable, its newest prerelease while
that is ahead of the stable, and ConcordeGo's latest significant update
(never a nightly, nothing older than 30 days). Each line: the date, the app
name linking to its card, "<version> now available", and one to three short
features. Builds that
share a title on one day are one line. The
features are Claude's (rules in `news-prompt.md`, cached under "news" in
`release-notes.auto.json`); until a release has one, the keyword ranking's
bold leads stand in. ConcordeGo is listed only when Claude judges an update
in its change list significant, with the date it went live; there is no
fallback for it. `sync-releases.py` writes the region `releases:news`.

## The FAQ check

When a ConcordeAI or ConcordeVPN stable or prerelease comes out, the Sync
releases Action gives its notes and the whole FAQ to Claude
(`faq-audit.py`, rules in `faq-audit-prompt.md`). If an answer looks out of
date, it opens a GitHub issue on this repo titled "FAQ check: <app>
<version> may affect N answer(s)", with the question, what changed and a
suggested fix; GitHub emails the repo owner. Each release is checked once:
`faq-audit.json` lists the ones done (delete a line to check it again).
It never edits the FAQ itself. `--seed` marks every current release as
checked without calling anything.

## Version labels in the FAQ

An FAQ answer about a feature that is not in every build names the version
it needs, and `sync-releases.py` writes the words, so the page never sends
someone looking for a feature their build does not have, and nobody has to
remember to update it when a version is promoted:

    <strong class="since" data-since="vpn 1.4"></strong>          ends an answer
    <strong class="since inline" data-since="vpn 1.4"></strong>   mid-sentence

| The newest build that has it | End of answer | Inline |
|---|---|---|
| Stable | Available from 1.4. | (from 1.4) |
| Prerelease | Available from 1.4, in Prerelease for now. | (from 1.4, in Prerelease for now) |
| Nightly | Coming in 1.4; in the Nightly build for now. | (coming in 1.4; in Nightly for now) |
| none yet | Coming in 1.4. | (coming in 1.4) |

The product is `ai` or `vpn`; the version is compared number by number
(1.10 is newer than 1.4). If a product's releases cannot be read, its
labels are left exactly as they are. `--check` reports "faq labels" when a
label is out of date.

## The product windows

Each product card shows one window: `windows/ai-window.html`,
`windows/vpn-window.html` and `windows/go-window.html`. The first two are
the apps' own UIs, rebuilt as static HTML from each app's real page (its
markup and its own CSS rules, with fake data) rather than screenshots of
whatever happened to be on screen. They are HTML so they stay crisp at any
size and can be restaged without re-shooting. The AI window is the opening
screen of a new chat (greeting, starter chips, message box) with visual
effects on: the sidebar and message box are the app's frosted glass over a
backdrop: a still of a night skyline, `windows/backdrop.jpg` (swap that file
for any other photo and re-run `make-shots.py`). The VPN
window is the main window as ConcordeVPN 1.4 draws it, 460x856: connected
through New York, Balanced, with the speed and route cards.
ConcordeGo is a website, so its window is a plain browser frame (the address
bar reads go.flyconcordefly.com) round a capture of the live app,
`windows/go-capture.png`.

Three rules they exist to enforce:

1. **No IP address, ever.** A real screenshot of ConcordeVPN shows the live
   exit IP. Publishing that puts an exit node on a marketing page, which is
   what gets an address scanned and reputation-listed. The generated window
   omits the concept entirely — not a placeholder, absent.
2. **No real user data.** The conversation titles in the AI window's
   sidebar are invented and work-shaped. A real capture lists whatever you
   were actually asking about.
3. **ConcordeGo is captured signed out, in a fresh browser.** Signed in, the
   page carries the account's email, the owner's admin links and the count
   of searches left today; signed out it carries none of them. Never take
   it from your everyday browser.

### Sized to the text

The window sits beside its card's text, and a script on the page (the fit
script at the bottom of `index.html`) sizes it to that text: the text column
sets the card's height, and the image, with the line under it, fills that
height or the column's width, whichever runs out first — and on a short
screen no more than the screen can show whole (at 1366x657 all three
windows come out 451px tall; the sticky then keeps them beside the text).
At 1440x900 that is about 542x575 (AI), 351x653 (VPN) and 446x473 (Go).
Under 1180px the window moves above the text at the card's width, its image
capped at the screen height less 170px but never below 530px (a 450px
window), and never so tall that the window outgrows the screen under the
header: 597x633 on a 768x1024 tablet, 479x507 on a 1024x768 one, 293x310 on
an 844x390 phone. So restaging a window, or rewording a card, changes how
big the window draws; nothing needs editing for it, but look at the result
(`measure.mjs`, below).

### Regenerating

    python3 tools/make-shots.py

That captures each window with headless Chrome at 2x, then normalises them
all to **one geometry**: in each output the opaque window occupies exactly
rows 116–1416 (`WIN_H` 1300) inside the same 116px transparent margin on
all four sides — placed by one resize from the pixel-aligned capture, so the
edges land on pixel boundaries with the same phase in every image. The page
relies on that margin: the fit script centres what shows, not the image
box, and the Enlarge dialog sizes the window rather than the image; both
know the margin as `SHADOW`, and the stacked layout's CSS knows the
window's share of the image as 1.18 (1532/1300) — **change `MARGIN` or
`WIN_H` and you must change those with it.** `WIN_H` is 1300 because the desktop cards draw the
windows up to about 650 CSS px tall, and the assets stay 2x; `MARGIN` and
`FEATHER` (116 and 43) keep the proportions the first, 900px windows had
(80 and 30). The box-shadow reaches further than the margin; the outer 43px
of it fade the shadow to nothing instead of cutting it (a cut shows on the
page as a faint line). The script refuses a capture whose shadow runs off
the edge, so if it complains, enlarge that entry's size in `SOURCES`. Both
`.png` and `.webp` are written to `assets/`; set the `width`/`height`
attributes in `index.html` to the printed dimensions if they change (today
1458x1532 for AI and Go, 931x1532 for VPN) — they are what reserves each
image's box before it loads.

**`--virtual-time-budget` is not optional** (the script passes it). Without
it Chrome captures before the Google Fonts webfonts arrive and silently falls
back to system faces — the render looks subtly wrong and nothing warns you.

`--default-background-color=00000000` is what keeps the page transparent, which
is what lets the site's starfield show around the window instead of a flat box.

### Retaking `windows/go-capture.png`

The capture is the live app's second step, **What matters to you?**, signed
out, with the first-visit tour skipped: 1050x1075 CSS px taken at 2x, so
2100x2150 px, which is exactly the page area of `go-window.html` (a
1050x1113 window less its 38px title bar) drawn at the 2x `make-shots.py`
captures at. Two things set those numbers. **1050 wide:** below about
1024px the app's stepper no longer fits its header and cuts the last step
to "Anything els"; 1050 leaves it room. **2x:** a 1x capture is scaled up
inside the 2x window and reads soft beside the other two, most of all in
the Enlarge view. The window keeps the old 1000x1060 frame's proportions,
so `shot-go` still comes out 1458x1532. `measure.mjs` gives a fresh
browser profile every run, so it is always signed out and always gets the
tour:

    MEASURE_DPR=2 node tools/measure.mjs https://go.flyconcordefly.com/ 1050x1233 \
      '(async () => {
         const wait = (ms) => new Promise((r) => setTimeout(r, ms));
         const btn = (t) => [...document.querySelectorAll("button,a")]
           .find((b) => b.offsetParent !== null && b.innerText.trim() === t);
         await wait(2000);
         btn("Skip")?.click();            // the "How it works" tour
         await wait(600);
         btn("What matters").click();     // the stepper scrolls to step 2
         await wait(2000);
         const steps = document.querySelector(".rsteps");
         return { signedOut: !!btn("Sign in"),
                  stepsFit: !!steps && steps.scrollWidth <= steps.clientWidth };
       })()' /tmp/go-step2.png
    python3 -c "from PIL import Image; Image.open('/tmp/go-step2.png').convert('RGB').crop((0, 0, 2100, 2150)).save('tools/windows/go-capture.png', optimize=True)"

The viewport is 1233 tall and the shot is cropped to the top 1075 because
that puts STEP 2 OF 4 just under the app's header and leaves the app's
floating "Ask or wish" bar, which sits at the bottom of the viewport, out of
the frame. The expression must print `"signedOut": true` and
`"stepsFit": true` (if the app has grown its header, widen the capture and
`go-window.html` together, keeping the window at 1000:1060). Then look at
the PNG before running `make-shots.py`: the header shows **Sign in** and
nothing else about an account, the last step reads **Anything else** in
full, and there is no email address, admin link or searches-left count
anywhere in it.

### Checking a layout

    node tools/measure.mjs https://flyconcordefly.com 375x812 \
      'document.querySelector("#concordevpn .shot img").getBoundingClientRect().toJSON()' [shot.png]

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
