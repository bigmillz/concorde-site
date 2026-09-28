#!/usr/bin/env python3
"""Write the download blocks in index.html from GitHub Releases.

The site is static and the release data used to be typed in by hand, which
is how it came to advertise a five-week-old stable and an RC that no longer
existed. Now the blocks between the `<!-- releases:KEY -->` markers are
generated, and the page can only be behind until someone runs this.

    python3 tools/sync-releases.py            # rewrite index.html, say what changed
    python3 tools/sync-releases.py --check    # exit 1 if the page is behind (CI)

Needs the `gh` CLI (authenticated, or unauthenticated for the public repos)
and nothing but the standard library.

What is shown, per product:
  Stable    the newest non-draft, non-prerelease release
  RC/Beta   the newest prerelease not titled "nightly", only while it is
            newer than stable — once stable overtakes it, it disappears.
            Only ever one; labelled from its title ("1.3 beta 3" -> Beta 3)
  Nightly   the one rolling release tagged `nightly` (title "1.3 nightly
            <commit>"), shown whenever it exists — same version as the
            prerelease or not; with the commit it was built from and a
            "Built" date taken from when its files last changed
The version is read from the asset filename (`ConcordeAI-6.0.0.dmg` -> 6.0.0)
because that is the string people see on disk; release *names* are labels
and have drifted from the files before. Build numbers are not shown.
Dates are the release's publish time, UTC, same as the footer.

Each channel gets a "Release notes" dropdown of at most SHOW_MAX lines.
Its SOURCE is every note the channel stands for — a prerelease covers every
beta/RC cut since the last stable, a stable everything since the previous
stable, a nightly its own build — taken from release-notes.json where we
wrote bullets for a tag, otherwise from the GitHub body. What is SHOWN is
the first of:
  1. release-notes.json "lines" -> repo -> version: a hand-written summary
     of a whole line (never used for a nightly)
  2. release-notes.auto.json -> repo -> channel_key(): a summary of exactly
     these notes, written by Claude in the GitHub Action
     (tools/summarize-notes.py) or by hand — this script only reads it
  3. rank_notes(): a keyword ranking that needs nothing — new features,
     visible changes and speed first, polish and internals folded into one
     closing "Plus N smaller fixes and polish" line linked to GitHub

ConcordeGo is a website, not a release, so only its "Release notes" list
is generated (between `<!-- releases:go -->` markers; the rest of its card
is hand-written). Its source is ConcordeGo's own public change list,
go.flyconcordefly.com/changes (its repository is private), kept in
tools/go-changes.json because the feed holds only its last ~50 builds: the
notes cover the 30 days up to the newest build (go_channel()). It is
summarized and cached like any channel, under the cache key "concordego",
and its fallback is go_rank(). Anything that fails on the way leaves that
list exactly as it is.
"""
import datetime
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"

# key -> (repo, buttons). Buttons are (label, filename regex) in display
# order; a button whose file is missing from the release is skipped.
PRODUCTS = [
    ("ai", "bigmillz/concordeai", [
        ("macOS",   r"\.dmg$"),
        ("Win zip", r"-Windows\.zip$"),
        ("Win msi", r"\.msi$"),
    ]),
    ("vpn", "bigmillz/concordevpn-releases", [
        ("macOS",   r"\.dmg$"),
    ]),
]

ARROW = ('<svg class="arw" width="11" height="11" viewBox="0 0 12 12" fill="none" '
         'aria-hidden="true"><path d="M6 1v9M2.4 6.6 6 10.2l3.6-3.6" stroke="currentColor" '
         'stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>')
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class Incomplete(Exception):
    """This product's releases are not in a state we can render — almost
    always a window rather than a decision, because both nightly producers
    delete the rolling release and recreate it on every cut. The product's
    block is left exactly as it is and the OTHER product still updates."""


def gh(path):
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if out.returncode:
        sys.exit("gh api %s failed: %s" % (path, out.stderr.strip()))
    return json.loads(out.stdout)


NIGHTLY_TAG = "nightly"          # one rolling release per repo; its title carries the commit

# Products whose Prerelease (beta/RC) block is SUPPRESSED on the site, however
# many prereleases exist upstream. Empty as of 2026-09-20, when 1.4 beta 1 was
# cut: the VPN's beta block is visible again. Put a product key back in here to
# hide its Prerelease block; Nightly and Stable are unaffected either way.
HIDE_PRERELEASE = set()


def is_nightly(rel):
    return rel["tag_name"] == NIGHTLY_TAG or "nightly" in (rel.get("name") or "").lower()


def channels(repo):
    """stable, beta (or RC), nightly, and the release lines each stands for:
    beta_line is every prerelease since stable, stable_line is stable plus
    every prerelease that led to it. A beta counts only while it is newer
    than stable, so that block disappears the moment stable overtakes it;
    the nightly is shown whenever one exists."""
    rels = [r for r in gh("repos/%s/releases?per_page=100" % repo) if not r["draft"]]
    # CORRECTNESS-CRITICAL. The API sorts by (created_at, tag_name) desc, and
    # the mirror's releases all share one created_at — so without this the
    # order falls back to the tag STRING and "v99" sorts above "v100".
    rels.sort(key=lambda r: r["published_at"], reverse=True)
    stables = [r for r in rels if not r["prerelease"]]
    if not stables:
        raise Incomplete("no stable release")
    stable = stables[0]
    beta_line = [r for r in rels if r["prerelease"] and not is_nightly(r)
                 and r["published_at"] > stable["published_at"]]  # newest first
    beta = beta_line[0] if beta_line else None
    # What this stable brought: itself, plus every prerelease that led to it.
    # Its notes cover the whole span since the previous stable, the way the
    # prerelease's cover the span since this one.
    floor = stables[1]["published_at"] if len(stables) > 1 else ""
    stable_line = [r for r in rels if not is_nightly(r)
                   and floor < r["published_at"] <= stable["published_at"]]
    if len(stables) < 2 and len(rels) >= 100:
        print("  %s: 100 releases fetched and no previous stable among them —"
              " the stable's line may be truncated" % repo)
    # the rolling tag first; a numbered nightly only as long as no rolling one
    # exists. Shown whenever it exists — even at the same version as the
    # prerelease or stable, since it is the newest code either way.
    nightly = next((r for r in rels if r["tag_name"] == NIGHTLY_TAG), None) \
        or next((r for r in rels if r["prerelease"] and is_nightly(r)), None)
    return stable, beta, nightly, beta_line, stable_line


def stamp(rel):
    """When a release last changed: a rolling tag keeps its original
    published_at when its files are replaced, so look at the assets too."""
    return max([rel["published_at"]] + [a.get("updated_at") or "" for a in rel["assets"]])


def commit_of(rel):
    m = re.search(r"nightly\s+([0-9a-f]{7,40})", rel.get("name") or "", re.I)
    return m.group(1)[:7] if m else ""


CURATED = Path(__file__).with_name("release-notes.json")
# Summaries of whole channels, one per distinct set of source notes. Written
# by tools/summarize-notes.py in the GitHub Action (Claude), or by hand;
# only READ here, so a release script's local run never dirties the tree.
AUTO = Path(__file__).with_name("release-notes.auto.json")
# The instructions Claude summarizes with. Its hash is part of every cache
# key, so editing the prompt re-summarizes every channel on the next run.
PROMPT = Path(__file__).with_name("release-notes-prompt.md")
MODEL = "claude-opus-5-5"
SHOW_MAX = 5             # lines in a Release notes list, the closing "Plus…" line included


def inline(t):
    """Escape, then allow **bold** and `code` only."""
    t = html.escape(t, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    return t


BOILERPLATE = re.compile(r"^\*\*(macos|windows|linux)\*\*|^verify\b|^bring-your-own|^shasum ", re.I)


def body_items(md):
    """Every note in a release body, whole: its bullet items, or failing
    that the sentences of its first paragraph. Install instructions, the
    title line and URLs are dropped. This is the SOURCE — what Claude is
    given and what the cache key hashes — so nothing is shortened here."""
    md = (md or "").replace("\r\n", "\n")
    items = [re.sub(r"^\s*[-*]\s+", "", ln) for ln in md.split("\n") if re.match(r"^\s*[-*]\s+", ln)]
    if not items:
        paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", md) if p.strip()]
        paras = [p for p in paras if not BOILERPLATE.match(p) and not p.startswith("    ")]
        items = re.split(r"(?<=[.!?])\s+", paras[0]) if paras else []
        # "ConcordeVPN 1.3 beta (build 43)." is a title, not a note
        if items and re.match(r"^\**Concorde\w+\**\s+[\d.]+", items[0]):
            items = items[1:]
        items = [i for i in items if not re.search(r"https?://", i)]     # URLs are for the body, not a bullet
    out = []
    for it in items:
        it = " ".join(it.split())
        if BOILERPLATE.match(it) or re.match(r"^\*\*[^*]+\*\*$", it):   # a bold-only line is a title
            continue
        if it:
            out.append(it)
    return out


# Words that may lose their capital after "**Lead** — ". Anything else (a
# product, a platform, a place) keeps it: "**Windows.** Windows runs…" must
# not become "windows runs…".
LOWER_OK = set(("a an the it its this that these those there here your you now once each every "
                "all any after before when while what how ask asks more most some no one new "
                "open opens pick choose drop drag set turn").split())
CLEAN_MAX = 125          # a displayed heuristic bullet, in characters


def clean(it):
    """One note cut back for display: its first sentence in the house
    "**Lead** — rest" form, then cut back at a clause boundary (", " or
    "; ") if it is still long — never mid-phrase with an ellipsis unless no
    boundary exists, and never ending inside a parenthesis."""
    it = it.strip()
    lead = re.match(r"^\*\*([^*]+?)[.:]\*\*\s+(.+)$", it)        # "**Video.** Ask for…"
    if lead:
        rest = re.split(r"(?<=[.!?])\s+", lead.group(2))[0]
        first = rest.split(" ", 1)[0]
        if first.lower() in LOWER_OK:
            rest = rest[0].lower() + rest[1:]
        it = "**%s** — %s" % (lead.group(1).strip(), rest)
    else:
        it = re.split(r"(?<=[.!?])\s+", it)[0]                   # first sentence
        if len(it) > CLEAN_MAX:                                  # then its first clause,
            head = re.split(r"\s[:;]\s", it)[0]                  # unless that is just a bold lead
            it = head if len(re.sub(r"\W", "", head)) > 24 else it
    it = it.rstrip(".").strip()
    if len(it) > CLEAN_MAX:
        cuts = [m.start() for m in re.finditer(r"[,;] ", it[:CLEAN_MAX + 1]) if m.start() >= 40]
        # not after a half-list: "…takes voice input, on Intel" would end on
        # the first item of a prepositional phrase the cut chopped in two
        cuts = [c for c in cuts if not re.search(
            r"[,;—] (on|in|at|for|with|by|from|to|of|via|into)\b[^,;]*$", it[:c])]
        if cuts:
            it = it[:cuts[-1]]
        else:
            it = it[:CLEAN_MAX - 3].rsplit(" ", 1)[0] + "…"
    if it.count("(") > it.count(")"):                           # never end inside a parenthesis
        it = it[:it.rfind("(")].rstrip(" ,;:—…")
    if it.count("**") % 2:                                       # never leave a bold open
        it = it.replace("**", "")
    return it.rstrip(" .,;:—").strip()


def condense(md, limit=4):
    """A release body reduced to its first few notes, each cut for display."""
    return [c for c in (clean(i) for i in body_items(md)) if c][:limit]


def load_curated():
    return json.loads(CURATED.read_text(encoding="utf-8")) if CURATED.exists() else {}


def load_auto():
    """The summary cache; a broken file is reported and ignored, never fatal —
    the heuristic below still gives the page something sensible."""
    if not AUTO.exists():
        return {}
    try:
        data = json.loads(AUTO.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except ValueError as exc:
        # loud in the Action's log; the summarizer refuses to touch the file
        # until it parses again, so a hand edit is never overwritten
        print("  %s%s is not valid JSON (%s) — using the heuristic notes"
              % ("::warning::" if os.environ.get("GITHUB_ACTIONS") else "", AUTO.name, exc))
        return {}


def notes_items(repo, rel, curated):
    """(notes, curated?) for one release: ours when we wrote some for its tag
    — an entry, even an empty one, is the last word — else its GitHub body."""
    ours = (curated.get(repo) or {})
    if rel["tag_name"] in ours:
        return ours[rel["tag_name"]], True
    return body_items(rel.get("body") or ""), False


def release_version(rel):
    """X.Y.Z from the release's files — the same source the version on the
    page comes from; the title only as a fallback."""
    for a in rel.get("assets") or []:
        m = re.search(r"(\d+\.\d+\.\d+)", a["name"])
        if m:
            return m.group(1)
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", rel.get("name") or "")
    return m.group(1) if m else ""


def source_items(repo, rels, curated=None):
    """Every note a channel stands for, newest release first, exact repeats
    dropped: [{"tag", "text", "curated"}]. A prerelease covers its line since
    the last stable, a stable covers everything since the previous stable.
    This one list feeds the cache key, Claude and the heuristic alike."""
    curated = load_curated() if curated is None else curated
    # A PROMOTED BETA. A stable whose version already went out as a
    # prerelease in this span almost always re-describes that line in fresh
    # words, which exact-text de-duplication cannot catch — the page once
    # showed every 6.0.4 change twice, as the curated beta bullets and as the
    # stable's own paraphrase. But a stable can also ADD something its betas
    # never had (VPN 1.3, v48: the What's new box, the update dialog). So its
    # notes stay in the span, marked "restates": Claude is told they may
    # retell the betas and merges them, keeping what is new; only the keyword
    # fallback, which cannot tell a paraphrase from news, leaves them out.
    # Curated bullets for the stable's tag make them ordinary notes again.
    ours = curated.get(repo) or {}
    shipped_as_pre = {release_version(r) for r in rels if r.get("prerelease")}

    def restates(rel):
        return (not rel.get("prerelease") and rel["tag_name"] not in ours
                and release_version(rel) in shipped_as_pre)

    def gather(sources):
        got, seen = [], set()
        for rel in sources:
            notes, mine = notes_items(repo, rel, curated)
            for it in notes:
                k = re.sub(r"\W+", " ", it).strip().lower()
                if k and k not in seen:
                    seen.add(k)
                    got.append({"tag": rel["tag_name"], "text": it, "curated": mine,
                                "restates": restates(rel)})
        return got

    return gather(rels)


def prompt_version():
    try:
        return hashlib.sha256(PROMPT.read_bytes()).hexdigest()[:12]
    except OSError:
        return "no-prompt"


def channel_key(kind, items):
    """The cache key for one channel's notes: a hash of exactly what Claude
    would be asked to summarize, and how. Any change to the notes, the
    prompt file or MODEL gives a new key, so a summary can never outlive
    the notes it was written from."""
    doc = {"model": MODEL, "prompt": prompt_version(), "kind": kind,
           "notes": [[i["text"], bool(i.get("restates"))] for i in items]}
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def usable(entry):
    """A cache entry the page can show: not a recorded failure, and saying
    something."""
    return (isinstance(entry, dict) and not entry.get("failed")
            and isinstance(entry.get("bullets"), list)
            and all(isinstance(b, str) for b in entry["bullets"])
            and (any(b.strip() for b in entry["bullets"]) or entry.get("fold") is True))


# ---- the no-key fallback -------------------------------------------------
# What the page shows until Claude has summarized a channel (a release
# script's local sync, or no API key yet). Transparent keyword scoring:
# what a user would notice goes up, how the sausage was made goes down.
# Every rule is a (pattern, weight) pair; a note's score is the sum.
SCORES = [
    # new things
    (r"\b(new(?! york)|adds?|introduc\w*|gains?|can now|now (can|runs|works|shows|supports|offers|lets|checks|has)|"
     r"lets you|you can|support for|supports|ask for|paints?|renders?|export\w*|end to end)\b", 3),
    (r"^an? \w+", 2),                                # "A kill switch: …", "An automatic speed test"
    (r"^an? [\w-]+(?: [\w-]+)?:", 2),                # "A kill switch: …" is how a new thing is announced
    (r"^\*\*[^*]+\*\*", 2),                          # a bold lead is someone's headline
    (r"\b(set up|setup|first[- ]run|optional|automatic\w*|choose|yours|your own)\b", 2),
    # speed and performance
    (r"\b(fast|faster|fastest|twice as|\d+x|speed|performance|quicker|latency|throughput|responsive|instant\w*|lighter|"
     r"less memory|races?)\b", 2),
    # platforms
    (r"\b(windows|linux|arm|intel|amd|ios|iphone|ipad)\b", 3),
    # what you can see
    (r"\b(redesign\w*|window|dialog|card|button|slider|menu|icon|animation\w*|backdrop|layout|screen|panel|"
     r"tooltip|badge|settings|look|starfield|progress|title bar|glides?|popup|notifications?|dark mode|sections?)\b", 1),
    # fixes: worth knowing, not worth leading with
    (r"\b(fix(e[sd])?|bugs?|crash\w*|no longer|actually|is back|again|broke\w*|wrong|stuck|strand\w*|hang)\b", -1),
    # polish and wording
    (r"\b(realign\w*|align\w*|tweak\w*|nudge\w*|spacing|padding|pixels?|margins?|moves?|moved|polish\w*|"
     r"tidy|tidier|trim\w*|wording|typos?|renam\w*|footer|ring|clause|rounds?)\b", -2),
    (r"\b(says?|label\w*|explains?)\b", -1),
    # how the app is made
    (r"\b(review\w*|refactor\w*|(unit|regression|integration|smoke) tests?|test suite|internal|logs?|lint|ci|"
     r"pin bump|release gate|scripts?|dependenc\w*|deps|workflow|debug\w*|class collision|guard)\b", -4),
    (r"^tests?\b|\btests? (for|of|on|cover\w*)\b", -4),        # not "speed test" or "full test"
    (r"[\w-]+/[\w./-]+|\b[\w-]+\.(sh|py|js|mjs|json|ya?ml|md|swift|html|css|plist)\b", -4),   # paths and files
]
SCORES = [(re.compile(p, re.I), w) for p, w in SCORES]
MINOR = 0                # a note scoring below this is "smaller fixes and polish"
# "...and 15 more since v50": the body's own admission that it lists only some
MORE_MARKER = re.compile(r"^[.…\s]*and \d+ more\b", re.I)
# "Release 6.1 beta 1 (build 275)": a nightly built from a release commit
RELEASE_CUT = re.compile(r"^release\s+(\d+(?:\.\d+)*(?:\s+(?:beta|rc)\s*\d*)?)(?:\s*\(build \d+\))?\.?$", re.I)
LEAD_VERBS = set("add fix fixe remove show make let support new now stop keep use drop move bring turn "
                 "open close set get change update updated improve".split())
STOP = set("a an and the of to in on for with its it is at by or as from that this be are your you".split())


def score(text):
    return sum(w for pat, w in SCORES if pat.search(text))


def _words(text):
    words = re.findall(r"[a-z0-9]+", re.sub(r"\*\*", "", text.lower()))
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in STOP]


def _tokens(text):
    return set(_words(text))


def same_change(a, b):
    """Two notes near enough to be one change reported twice ("Settings gains
    Flush the DNS cache" in three builds, or reworded once), or a later note
    refining the same thing ("An automatic speed test in Performance mode" /
    "The automatic speed test waits until…": the same three-word subject)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    both = len(ta & tb)
    if both / len(ta | tb) >= 0.75 or (min(len(ta), len(tb)) >= 3 and both / min(len(ta), len(tb)) >= 0.85):
        return True
    # the shared subject must be a thing, not a verb: "Adds X to Y" and
    # "Adds X to Z" are two changes
    wa, wb = _words(a), _words(b)
    return (len(wa) >= 4 and len(wb) >= 4 and wa[:3] == wb[:3]
            and wa[0] not in LEAD_VERBS)


def rank_notes(items, kind, cap=SHOW_MAX):
    """(bullets, closing line or None) for a channel without a Claude
    summary. Near-repeats are merged, notes are ordered by score (newest
    first among equals), the top few shown and the rest folded into one
    closing line. A stable or prerelease shows only notes a user would
    notice while it has any; a nightly — what is being worked on right
    now — shows the top of whatever there is. Hand-written notes that
    already fit are shown exactly as written. Deterministic."""
    # a keyword ranking cannot tell a promoted stable's paraphrase from news
    items = [i for i in items if not i.get("restates")] or items
    unlisted, cuts, cands = False, [], []
    for it in items:
        text = it["text"].strip()
        if MORE_MARKER.match(text):
            unlisted = True
            continue
        m = RELEASE_CUT.match(text)
        if m:
            cuts.append(m.group(1))
            continue
        shown = text.rstrip(".") if it.get("curated") else clean(text)   # ours are written for the page already
        if not shown:
            continue
        if any(same_change(text, c["src"]) for c in cands):
            continue
        cands.append({"src": text, "text": shown, "score": score(text), "curated": it.get("curated")})
    if not cands:
        return (["Same code as %s" % cuts[0]] if cuts else []), None
    if all(c["curated"] for c in cands) and len(cands) <= cap and not unlisted:
        return [c["text"] for c in cands], None

    ranked = sorted(cands, key=lambda c: -c["score"])            # stable: newest first among equals
    # Only what a user would notice, while there is any. A nightly with
    # nothing but fixes still names its top two: it is what is being worked
    # on right now, and "smaller fixes" alone would say nothing.
    eligible = [c for c in ranked if c["score"] >= MINOR] or ranked[:2]
    if len(eligible) == len(cands) <= cap and not unlisted:
        return [c["text"] for c in eligible], None
    shown = eligible[:cap - 1]
    folded = [c for c in cands if c not in shown]
    minor = all(c["score"] < MINOR for c in folded)
    if unlisted:        # the body said "and N more": the true count is unknown
        more = "Plus smaller fixes and polish" if minor else "Plus more changes"
    elif len(folded) == 1:
        more = "Plus one smaller fix" if minor else "Plus one more change"
    else:
        more = ("Plus %d smaller fixes and polish" if minor else "Plus %d more changes") % len(folded)
    return [c["text"] for c in shown], more


def render_notes(repo, bullets, more=None, link=True):
    lis = ["<li>%s</li>" % inline(b) for b in bullets]
    if more and not link:
        # ConcordeGo: its history is on an unmerged branch of another repo —
        # nothing a visitor should be sent to
        lis.append('<li class="more">%s</li>' % html.escape(more))
    elif more:
        # the whole releases list, not one tag: a channel's notes span
        # several releases, and no single tag page shows all of them
        url = "https://github.com/%s/releases" % repo
        lis.append('<li class="more">%s &mdash; <a href="%s">full notes on GitHub</a></li>'
                   % (html.escape(more), html.escape(url)))
    return "<ul>%s</ul>" % "".join(lis) if lis else ""


def notes_for(repo, rels, version="", kind="stable"):
    """One bullet list for a channel, covering every release it stands for.
    First of these wins:

      1. release-notes.json -> "lines" -> repo -> version: a hand-written
         summary of the whole line. Only for a channel standing for a line
         (`version` given) — never a nightly, which is one build.
      2. release-notes.auto.json -> repo -> channel_key(): the Claude (or
         hand-edited) summary of exactly these notes.
      3. rank_notes(): the keyword heuristic, which needs nothing."""
    curated = load_curated()
    summary = ((curated.get("lines") or {}).get(repo) or {}).get(version) if version else None
    if summary:
        return render_notes(repo, summary)
    items = source_items(repo, rels, curated)
    if not items:
        return ""
    got = cached_notes(repo, kind, items)
    if got is not None:
        return got
    bullets, more = rank_notes(items, kind)
    return render_notes(repo, bullets, more)


def cached_notes(repo, kind, items, link=True):
    """The rendered cache entry for exactly these notes, or None."""
    entry = (load_auto().get(repo) or {}).get(channel_key(kind, items))
    if not usable(entry):
        return None
    bullets = [b for b in entry["bullets"] if b.strip()]
    more = None
    if entry.get("fold"):
        more = "Plus smaller fixes and polish" if bullets else "Smaller fixes and polish"
    return render_notes(repo, bullets, more, link)


# ---- ConcordeGo ------------------------------------------------------------
# A website that deploys continuously, with no releases: its notes are the
# changes in the GO_DAYS up to its newest build. The window is anchored to
# that build's own date, so the notes (and their cache key) change only when
# Go deploys, never just because a day went by.
GO_URL = "https://go.flyconcordefly.com/"
# ConcordeGo's public change list (2026-09-27: its repository went private,
# so the site reads no git history). Newest first, ~50 builds deep:
#   GET /changes?since=<commit>&limit=<n>
#   {"changes": [{"version": "0.1.4218", "commit": "7134ae2", "date": "2026-09-27", "summary": "..."}],
#    "since_found": true}
GO_CHANGES_URL = GO_URL + "changes"
# Everything the feed has shown, so the 30-day window outlives its ~50
# builds. Written only in the GitHub Action (committed with the page), so a
# release script's local sync never leaves the site checkout dirty; seeded
# 2026-09-27 from the history ConcordeGo had in bigmillz/concordeai.
GO_STORE = Path(__file__).with_name("go-changes.json")
GO_KEY = "concordego"                    # its own key in release-notes.auto.json, so pruning
                                         # the AI's summaries never touches Go's
GO_KIND = "live"
GO_DAYS = 30
GO_CAP = 150                             # newest builds sent on; the rest become an "...and N more" note
GO_TIMEOUT = 10                          # seconds, per network operation


# ConcordeGo's version, "0.1.<build>" (the build counts every commit), from
# the site's public /version endpoint:
#   {"product": "ConcordeGo", "version": "0.1.4075", "channel": "beta", "updated": "2026-09-27"}
# written into the Go card on every sync (worker.js also stamps it live).
GO_VERSION_URL = GO_URL + "version"
GO_VERSION_RX = re.compile(r"^\d+\.\d+\.\d+$")


def go_version_info(url=GO_VERSION_URL):
    """{version, updated[, commit]} from /version, or None: anything missing,
    malformed or unreachable is None, never an error on the page."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "concorde-site sync-releases",
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=GO_TIMEOUT) as r:
            j = json.loads(r.read(64 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:    # 404 until the endpoint is deployed
        exc.close()
        return None
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    version, updated = str(j.get("version") or "").strip(), str(j.get("updated") or "").strip()
    if not GO_VERSION_RX.match(version) or len(version) > 20:
        return None
    try:
        if datetime.date.fromisoformat(updated).isoformat() != updated:
            return None
    except ValueError:
        return None
    out = {"version": version, "updated": updated}
    commit = str(j.get("commit") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{7,40}", commit):
        out["commit"] = commit
    return out


def stamp_go_version(page, info):
    """The Go card's version number and "Updated" date from `info`; the
    number is shown (its `hidden` dropped) once a version is known."""
    shown = pretty(info["updated"])
    page = re.sub(r'<span class="chan-commit" data-go-vline(?: hidden)?>',
                  '<span class="chan-commit" data-go-vline>', page)
    page = re.sub(r'<span data-go-version>[^<]*</span>',
                  '<span data-go-version>%s</span>' % html.escape(info["version"]), page)
    page = re.sub(r'<time datetime="[^"]*" data-go-date>[^<]*</time>',
                  '<time datetime="%s" data-go-date>%s</time>' % (info["updated"], shown), page)
    return page


GO_PREFIX = re.compile(r"^ConcordeGo\b:?\s*")


def go_subject(message):
    """A commit's subject line as a note: "ConcordeGo: " dropped (a scope
    such as "Flight Fixer:" or "deploy:" kept — it says what was touched),
    first letter up."""
    s = " ".join((message or "").split("\n", 1)[0].split())
    s = GO_PREFIX.sub("", s)
    if s[:1].islower() and not re.match(r"^[a-z]+[A-Z]", s):     # not "iPhone"
        s = s[0].upper() + s[1:]
    return s


def load_go_store():
    try:
        got = json.loads(GO_STORE.read_text(encoding="utf-8")).get("changes")
        return [c for c in got if isinstance(c, dict) and c.get("commit")] if isinstance(got, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def fetch_go_changes(since=None):
    """(changes, since_found) from the feed; raises Incomplete on anything
    unexpected, so the page is left as it is."""
    q = {"limit": "200"}
    if since:
        q["since"] = since
    url = GO_CHANGES_URL + "?" + urllib.parse.urlencode(q)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "concorde-site sync-releases",
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=GO_TIMEOUT) as r:
            j = json.loads(r.read(2 * 1024 * 1024).decode("utf-8"))
    except Exception as exc:
        raise Incomplete("ConcordeGo's change list could not be read (%s)" % (str(exc).strip() or type(exc).__name__))
    if not isinstance(j, dict) or j.get("error") or not isinstance(j.get("changes"), list):
        raise Incomplete("ConcordeGo's change list answered %s" % str(j.get("error") if isinstance(j, dict) else "oddly")[:80])
    good = []
    for c in j["changes"]:
        if not isinstance(c, dict):
            continue
        commit = str(c.get("commit") or "").strip().lower()
        date = str(c.get("date") or "").strip()
        summary = str(c.get("summary") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{7,40}", commit) or not summary or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        v = c.get("version")
        good.append({"commit": commit[:7], "version": v if isinstance(v, str) and GO_VERSION_RX.match(v) else None,
                     "date": date, "summary": summary[:300]})
    return good, bool(j.get("since_found"))


def go_channel():
    """What the Go notes are made from: {build, source, date, since, label,
    items, total, changes}. `changes` is the merged list to store (the
    caller writes it, in the Action only). Raises Incomplete when the feed
    cannot be read; the page is then left exactly as it is."""
    store = load_go_store()
    fresh, _found = fetch_go_changes(store[0]["commit"] if store else None)
    known = {c["commit"] for c in store}
    merged = [c for c in fresh if c["commit"] not in known] + store    # newest first; since_found false = match on commit
    if not merged:
        raise Incomplete("ConcordeGo's change list is empty")
    head = merged[0]
    newest = datetime.date.fromisoformat(head["date"])
    since = (newest - datetime.timedelta(days=GO_DAYS)).isoformat()
    window = [c for c in merged if c["date"] >= since]
    items, seen = [], set()
    for c in window:
        text = go_subject(c["summary"])
        k = re.sub(r"\W+", " ", text).strip().lower()
        if k and k not in seen:
            seen.add(k)
            items.append({"tag": c["commit"], "text": text, "curated": False, "restates": False})
    total = len(items)
    if total > GO_CAP:
        items = items[:GO_CAP] + [{"tag": "older", "text": "...and %d more since %s" % (total - GO_CAP, since),
                                   "curated": False, "restates": False}]
    label = ("version %s, deployed %s" % (head["version"], pretty(head["date"])) if head.get("version")
             else "build %s, deployed %s" % (head["commit"], pretty(head["date"])))
    return {"build": head["commit"], "source": "live", "date": head["date"], "since": since,
            "label": label, "items": items, "total": total, "changes": window}


def save_go_store(changes):
    """Keep the window's changes; only in the GitHub Action (see GO_STORE)."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return False
    data = {"_": "ConcordeGo's changes as go.flyconcordefly.com/changes listed them, newest first. "
                 "Written by tools/sync-releases.py in the Sync releases Action; the Release notes "
                 "summarize the last 30 days of it.", "changes": changes}
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    try:
        if GO_STORE.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    GO_STORE.write_text(text, encoding="utf-8")
    return True


# The keyword fallback for Go. Commit subjects are terser than release
# notes, and one subject often carries several changes ("lounge chips;
# narrower Fixer odds; memory on the admin page"), so each is judged
# clause by clause: a clause about running or building the site is dropped
# whole, polish scores down, the things a visitor comes for score up.
GO_INTERNAL_SCOPE = re.compile(
    r"^(deploy\S*|admin|server|live(\.py)?|[\w-]+\.(sh|py)|google adapter|mockups?|mock \d+|capture|wants)\b", re.I)
GO_SCORES = [
    # running the site and building it: a clause with any of these is dropped
    (r"\b(admin|allowlist|tests?|pin(s|ned)?|research\w*|tables?|records?|CLAUDE\.md|deploy\w*|droplets?|"
     r"launch(er|agent)|updater|update script|units|firewall|root|tunnel|zones?|token|account id|zero trust|"
     r"access|adapter|capture|fixtures?|corpus|mock(s|ups?)?|proposal|design round|directions|article|"
     r"build stamp|rebuild|credits?|footers?|parser|imports?|stdin|heredoc|doctl|dns|venv|syntax|verifier|"
     r"parameter|keys?|quota|cache[sd]?|counters|metered|serpapi|supplement|feed'?s?|provider|enrichment|"
     r"scorer|narrator|draft|brief|post|404|recordings?|ceiling|rate floor|safe\.directory|script|"
     r"interface|pexels|aerodatabox|skytrax|duffel|price_insights|mb|per patrick|patrick|pat|e-?mails?|"
     r"allowances?|owners?|staging)\b", -5),
    # the features a visitor comes for
    (r"\b(points and miles|flight fixer|round-trip fares|ratings|rated|price history|nearby days|quiz|profile|"
     r"autocomplete|award charts?|transfer planner|price signal|exchange rates|bag fees|"
     r"delayed-or-cancelled|chance of flying)\b", 4),
    # …and what they are made of
    (r"\b(grade[sd]?|grading|points|miles|fares?|prices?|priced|bags?|cabins?|airlines?|layovers?|lounge|"
     r"wifi|meals?|legroom|searches|results|signed|account)\b", 1),
    # polish: how it looks, not what it does
    (r"\b(blocks?|scale|buttons?|colou?rs?|icons?|gutters?|align\w*|centred|centered|shadow|tiles?|flash|"
     r"captions?|panels?|scrollbar|copy|tone|dropdowns?|rows?|grid|lockup|wordmark|figures|seal|podium|"
     r"spacing|heading|pill|tidied|chip counts|named like|fields|marked|trip type|validation|wizard is hidden)\b", -3),
]
GO_SCORES = [(re.compile(p, re.I), w) for p, w in GO_SCORES]
GO_INTERNAL = GO_SCORES[0][0]
# Never published from a commit subject, whatever else the clause says: an
# address, a domain, an IP (the Claude path rejects these in tidy())
GO_PRIVATE = re.compile(r"@|https?://|www\.|\b[a-z0-9-]+\.(com|net|org|io|app|dev|ai|co|me|sh|xyz)\b|"
                        r"\b\d{1,3}(\.\d{1,3}){3}\b", re.I)
GO_GROUPS = re.compile(r"\b(flight fixer|round-trip)\b", re.I)    # many commits, one feature each
GO_GROUP_LEAD = {"flight fixer": "Flight Fixer", "round-trip": "Round-trip fares"}
# Go's commit subjects are written in British spelling; the page is American
GO_US = {"modelled": "modeled", "modelling": "modeling", "colour": "color", "colours": "colors",
         "programme": "program", "programmes": "programs", "centre": "center", "centred": "centered",
         "favourite": "favorite", "favourites": "favorites", "itemised": "itemized", "travelled": "traveled",
         "traveller": "traveler", "travellers": "travelers", "cheque": "check", "licence": "license"}
GO_US_WORD = re.compile(r"\b(%s)\b" % "|".join(GO_US), re.I)


def go_us(text):
    return GO_US_WORD.sub(lambda m: (GO_US[m.group(1).lower()] if m.group(1).islower()
                                     else GO_US[m.group(1).lower()].capitalize()), text)


def go_score(text):
    return score(text) + sum(w for pat, w in GO_SCORES if pat.search(text))


def go_clauses(text):
    """(lead, [clauses]) of one subject: its scope, when it is one a visitor
    would know ("Flight Fixer"), and the clauses of its first sentence. A
    scope about running or building the site gives no clauses at all."""
    lead = None
    m = re.match(r"^([A-Za-z][\w .,/-]{0,28}?):\s+(.+)$", text)
    if m:
        scope, text = m.group(1), m.group(2)
        if GO_INTERNAL_SCOPE.match(scope) or GO_INTERNAL.search(scope):
            return None, []
        if re.match(r"^flight fixer$", scope, re.I):
            lead = "Flight Fixer"
    elif re.match(r"^(mock(ups?| \d+)|deploy\S*|admin|server)\b", text, re.I):   # "Mock 10, the first screen…"
        return None, []
    text = re.split(r"(?<=[a-z0-9)\"'])\.\s+(?=[A-Z])", text)[0]       # the first sentence
    clauses = [c.strip().rstrip(".") for c in re.split(r";\s+", text) if c.strip()]
    return lead, clauses


def go_rank(items, cap=SHOW_MAX):
    """(bullets, closing line or None) for the Go notes without a Claude
    summary. Deterministic. Clauses a visitor would not see are dropped
    whole and the rest shown as whole clauses, never cut mid-phrase. One
    line per change: every commit about one feature ("Flight Fixer") is
    one candidate, shown by the earliest that says something a visitor
    would see — the one that brought it — at the best score any of them
    got; other near-repeats are merged too.
    The top few are shown by score, newest first among equals."""
    unlisted, cands = False, []
    for n, it in enumerate(items):
        text = it["text"].strip()
        if MORE_MARKER.match(text):
            unlisted = True
            continue
        lead, clauses = go_clauses(text)
        kept = [(c, go_score(c)) for c in clauses]
        kept = [(c, s) for c, s in kept if s >= MINOR and not GO_INTERNAL.search(c) and not GO_PRIVATE.search(c)]
        if not kept:
            continue                                  # folded
        best = max(s for _c, s in kept)
        # lead with the best clause, then whatever else fits whole
        kept.sort(key=lambda cs: -cs[1])
        shown = [kept[0][0]]
        for c, _s in kept[1:]:
            if len("; ".join(shown + [c])) <= CLEAN_MAX:
                shown.append(c)
        body = go_us(clean("; ".join(shown)))
        group = GO_GROUPS.search(text)
        group = group.group(1).lower() if group else None
        boost = 4 if lead else 0
        if not lead and group and group not in body.lower():
            lead = GO_GROUP_LEAD[group]              # the feature's clause lost to a better one: name it
        if lead:                                     # "**Flight Fixer** — the odds…"
            if body.split(" ", 1)[0].lower() in LOWER_OK:
                body = body[0].lower() + body[1:]
            body = "**%s** — %s" % (lead, body)
        elif body[:1].islower():
            body = body[0].upper() + body[1:]
        cands.append({"text": body, "src": "; ".join(shown), "n": n, "score": best + boost, "group": group})
    # one per group: the earliest commit (newest first, so the last seen),
    # carrying the group's best score
    best_of = {}
    for c in cands:
        g = c["group"]
        if g:
            top = max(c["score"], best_of[g]["score"]) if g in best_of else c["score"]
            best_of[g] = c
            c["score"] = top
    ranked = sorted((c for c in cands if not c["group"] or best_of[c["group"]] is c),
                    key=lambda c: (-c["score"], c["n"]))
    picked = []
    for c in ranked:
        if not any(same_change(c["src"], p["src"]) for p in picked):
            picked.append(c)
    total = len(items) - (1 if unlisted else 0)
    if len(picked) == total <= cap and not unlisted:
        return [c["text"] for c in picked], None
    shown = picked[:cap - 1]
    # Commits are not changes, so no count ("Plus 166 more changes" would
    # mislead); a commit merged into a shown line is part of that line.
    more = len(picked) > len(shown)
    return [c["text"] for c in shown], ("Plus more changes" if more else "Plus smaller fixes and polish")


def go_notes(go):
    """The Go notes list: the cached summary of exactly these notes, else
    the keyword fallback. Never a link to GitHub."""
    items = go["items"]
    if not items:
        return ""
    got = cached_notes(GO_KEY, GO_KIND, items, link=False)
    if got is not None:
        return got
    bullets, more = go_rank(items)
    return render_notes(GO_KEY, bullets, more, link=False)


def pretty(iso):
    y, m, d = iso[:10].split("-")
    return "%d %s %s" % (int(d), MONTHS[int(m) - 1], y)


def display_version(v):
    """As the apps show it: one trailing ".0" dropped — 1.3.0 -> 1.3, 6.0.0 -> 6.0."""
    return v[:-2] if v.count(".") >= 2 and v.endswith(".0") else v


def channel_html(name, rel, buttons, repo, tag="", notes_rels=None):
    picks = []
    for label, pat in buttons:
        asset = next((a for a in rel["assets"] if re.search(pat, a["name"])), None)
        if asset:
            picks.append((label, asset))
    if not picks:
        # the upload window of a delete-then-create nightly, usually
        raise Incomplete("%s (%s) has none of the expected assets"
                         % (name, rel["tag_name"]))
    # .sha256 files still ship with every release; they are just not put in
    # front of someone who came to download an app (Pat, 2026-09-18).
    m = re.search(r"(\d+\.\d+\.\d+)", picks[0][1]["name"])
    version = m.group(1) if m else rel["name"]
    date = (stamp(rel) if is_nightly(rel) else rel["published_at"])[:10]

    lines = ['            <div class="chan">',
             '              <div class="chan-top">',
             '                <span class="chan-name">%s</span>' % name,
             '                <span class="chan-ver">%s</span>' % html.escape(display_version(version))]
    if tag:                                                  # "beta 1" / "RC 1", after the version
        lines.append('                <span class="chan-commit">%s</span>' % tag)
    if is_nightly(rel):
        # data-nightly-*: worker.js re-reads the nightly's commit and date
        # from GitHub at request time and rewrites these two, so the page
        # is never behind on a nightly even before the next sync
        key = "ai" if repo == "bigmillz/concordeai" else "vpn"
        commit = commit_of(rel)
        base = "" if repo.endswith("-releases") else "https://github.com/%s/commit/" % repo
        if commit and base:                                  # a public repo: link the commit
            commit = '<a href="%s%s">%s</a>' % (base, commit, commit)
        # data-nightly-ver lets the Worker check that the live nightly is
        # still THIS version before it rewrites anything: the download
        # buttons below are generated and it cannot rewrite those, so a
        # version bump must leave the whole block alone rather than make a
        # stale block look freshly built while its download 404s.
        lines.append('                <span class="chan-commit" data-nightly-commit="%s" data-nightly-ver="%s"%s>%s</span>'
                     % (key, html.escape(display_version(version)),
                        ' data-commit-base="%s"' % base if base else "",
                        # "build <sha>": the word in the same small print (Pat, 2026-09-27)
                        "build " + commit if commit else "&mdash;"))
        lines += ['                <span class="chan-date">Built <time datetime="%s" data-nightly-date="%s">%s</time></span>'
                  % (date, key, pretty(date)),
                  '              </div>']
    else:
        lines += ['                <span class="chan-date">Released <time datetime="%s">%s</time></span>'
                  % (date, pretty(date)),
                  '              </div>']
    lines += [
             '              <div class="btns">']
    for label, asset in picks:
        lines += ['                <a class="btn" href="%s">' % html.escape(asset["browser_download_url"]),
                  '                  <span class="os">%s</span>' % label,
                  '                  <span class="file">%s</span>' % html.escape(asset["name"]),
                  '                  ' + ARROW,
                  '                </a>']
    lines.append('              </div>')
    # The line key is only meaningful for a channel that STANDS FOR a line
    # (stable, prerelease). A nightly is one build and speaks for itself —
    # without this it would inherit the line summary of whatever version it
    # happens to share a number with.
    kind = name.lower()                  # stable / prerelease / nightly
    notes = notes_for(repo, notes_rels or [rel],
                      display_version(version) if notes_rels else "", kind)
    if notes:
        lines += ['              <details class="sum notes">',
                  '                <summary>Release notes</summary>',
                  '                <div class="sum-body">',
                  notes,
                  '                </div>',
                  '              </details>']
    lines.append('            </div>')
    return "\n".join(lines)


def channel_specs(key, repo):
    """What the page shows for one product, in order: [(name, release, tag,
    releases its notes stand for)]. The summarizer walks the same list, so
    it summarizes exactly the channels the page carries."""
    stable, beta, nightly, beta_line, stable_line = channels(repo)
    specs = [("Stable", stable, "", stable_line)]
    if key in HIDE_PRERELEASE:
        beta = None                      # see HIDE_PRERELEASE above
    if beta:
        # Apple-style numbering from the title, shown after the version:
        # "1.3 beta 3" -> Prerelease 1.3 beta 3; "1.3 RC1" -> RC 1. An
        # unnumbered title is the first beta of its line.
        name = beta["name"] or ""
        m = re.search(r"\bRC\s*(\d+)?", name, re.I)
        if m:
            tag = "RC " + (m.group(1) or "1")
        else:
            m = re.search(r"\bbeta\s+(\d+)\b", name, re.I)
            tag = "beta " + (m.group(1) if m else "1")
        specs.append(("Prerelease", beta, tag, beta_line))
    if nightly:
        specs.append(("Nightly", nightly, "", None))
    return specs


def block(key, repo, buttons):
    specs = channel_specs(key, repo)
    parts = [channel_html(name, rel, buttons, repo, tag, notes_rels=line)
             for name, rel, tag, line in specs]
    summary = " + ".join(
        ("nightly %s" % (commit_of(rel) or rel["tag_name"])) if name == "Nightly"
        else "%s (%s)" % (rel["tag_name"], rel["name"])
        for name, rel, _tag, _line in specs)
    return "\n\n".join(parts) + "\n", summary


CHAN_NAME = '<span class="chan-name">'
# Markers are matched exactly, indent included: twelve spaces, except Go's
# notes list, which sits deeper in its card.
MARKER_INDENT = {"go": 20}


def region(page, key):
    """The generated span for one product, as (start, end) offsets. Exactly
    one marker pair must exist: with two, there is no safe place to write."""
    pad = " " * MARKER_INDENT.get(key, 12)
    begin, end = "%s<!-- releases:%s -->\n" % (pad, key), "%s<!-- /releases:%s -->" % (pad, key)
    if page.count(begin) != 1 or page.count(end) != 1:
        sys.exit("index.html: %s has %d open / %d close markers, expected 1 each"
                 % (key, page.count(begin), page.count(end)))
    i = page.index(begin) + len(begin)
    return i, page.index(end, i)


def repeats(page, key):
    """Channel names appearing more than once in one product's block. A
    merge can produce this without a conflict — two syncs that each INSERT
    the same block at the same anchor replay as two insertions — which is
    how the page once shipped a channel twice. Generation cannot."""
    i, j = region(page, key)
    names = re.findall(r'<span class="chan-name">(.*?)</span>', page[i:j])
    return sorted({n for n in names if names.count(n) > 1})


def page_problem(page=None):
    """Why index.html cannot be written, or None: a missing or doubled
    marker pair, or a channel block outside the generated regions. The
    summarizer asks first, so it never pays for summaries a sync that is
    bound to fail could not ship."""
    page = INDEX.read_text(encoding="utf-8") if page is None else page
    try:
        inside = sum(page[slice(*region(page, k))].count(CHAN_NAME) for k, _r, _b in PRODUCTS)
        gi, gj = region(page, "go")
    except SystemExit as exc:
        return str(exc)
    if page.count(CHAN_NAME) != inside:
        return "index.html: %d channel block(s) outside the releases markers" % (page.count(CHAN_NAME) - inside)
    # Go's notes region is one list inside the Go card's Release notes: not
    # in another card, not wrapped round anything else
    card = page.find('id="concordego"')
    card_end = page.find("</article>", card)
    if card < 0 or not card < gi < card_end or card_end < gj:
        return "index.html: the releases:go markers are not inside the ConcordeGo card"
    if re.search(r"<(?!/?(ul|li|b|code|a)\b)[a-z/!]", page[gi:gj]) or page[gi:gj].count("<ul>") != 1:
        return "index.html: the releases:go region must hold exactly one notes list"
    # …and it is the card's only one: no marker at another indent, no second
    # list or Release notes box beside the generated one
    if len(re.findall(r"<!--\s*releases:go\s*-->", page)) != 1 or len(re.findall(r"<!--\s*/releases:go\s*-->", page)) != 1:
        return "index.html: releases:go markers outside the generated pair"
    if page[card:card_end].count("<ul>") != 1 or page[card:card_end].count('<details class="sum notes">') != 1:
        return "index.html: the ConcordeGo card has a notes list outside the releases:go markers"
    return None


# ---- "Available from" labels ------------------------------------------------
# An FAQ answer about a feature that is not in every build ends with
#   <strong class="since" data-since="vpn 1.4"></strong>   "Available from 1.4."
# or carries one mid-sentence as
#   <strong class="since inline" data-since="vpn 1.4"></strong>   "(from 1.4)"
# and this fills in the words from the releases as they stand, so a feature
# is never described as out before it is, and the label moves on by itself
# when its version reaches Prerelease or Stable (Pat, 2026-09-27: "so it
# doesn't slip between the cracks"). Only the words between the tags are
# written; the version a feature needs is the one thing set by hand.
SINCE = re.compile(r'(<strong class="since( inline)?" data-since="(ai|vpn) (\d+(?:\.\d+)*)">)(.*?)(</strong>)', re.S)


def _vtuple(v):
    nums = [int(x) for x in re.findall(r"\d+", v or "")][:3]
    return tuple(nums + [0] * (3 - len(nums)))


def since_label(need, stable, beta, nightly, inline=False):
    """The words for one label, from the versions each channel carries now."""
    want, shown = _vtuple(need), display_version(need)
    if stable and _vtuple(stable) >= want:
        words = ("Available from %s", "from %s")
    elif beta and _vtuple(beta) >= want:
        words = ("Available from %s, in Prerelease for now", "from %s, in Prerelease for now")
    elif nightly and _vtuple(nightly) >= want:
        words = ("Coming in %s; in the Nightly build for now", "coming in %s; in Nightly for now")
    else:
        words = ("Coming in %s", "coming in %s")
    return "(%s)" % (words[1] % shown) if inline else "%s." % (words[0] % shown)


def stamp_since(page):
    """(page, changed): every since label rewritten from the live releases.
    A product whose releases cannot be read keeps its labels as they are."""
    versions = {}
    for key, repo, _buttons in PRODUCTS:
        if not re.search(r'data-since="%s ' % key, page):
            continue
        try:
            stable, beta, nightly, _bl, _sl = channels(repo)
        except (Incomplete, SystemExit) as exc:
            print("  %-4s labels: releases unreadable (%s) — leaving them as they are" % (key, exc))
            continue
        versions[key] = tuple(release_version(r) if r else "" for r in (stable, beta, nightly))

    def fill(m):
        # an example inside an HTML comment (the FAQ's own how-to) is not a label
        if m.group(3) not in versions or page.rfind("<!--", 0, m.start()) > page.rfind("-->", 0, m.start()):
            return m.group(0)
        return (m.group(1) + since_label(m.group(4), *versions[m.group(3)], inline=bool(m.group(2)))
                + m.group(6))

    new = SINCE.sub(fill, page)
    return new, new != page


def main():
    check = "--check" in sys.argv
    page = INDEX.read_text(encoding="utf-8")
    behind, incomplete = [], []
    for key, repo, buttons in PRODUCTS:
        try:
            probe = block(key, repo, buttons)
        except Incomplete as exc:
            print("  %-4s %s — leaving its block untouched" % (key, exc))
            incomplete.append(key)
            continue
        was = repeats(page, key)
        if was:
            print("  %-4s repeating %s — regenerating fixes it" % (key, ", ".join(was)))
        i, j = region(page, key)
        new, summary = probe
        if page[i:j] != new:
            behind.append(key)
            page = page[:i] + new + page[j:]
        elif was:
            behind.append(key)
        print("  %-4s %s%s" % (key, summary, "" if key in behind else "  (unchanged)"))
    # ConcordeGo's notes list. Anything unreadable leaves it exactly as it is,
    # and an empty result never replaces it.
    try:
        go = go_channel()
    except Incomplete as exc:
        print("  go   %s — leaving its notes untouched" % exc)
        go = None
    if go and not check and save_go_store(go["changes"]):
        print("  go   stored %d change(s) in %s" % (len(go["changes"]), GO_STORE.name))
    if go:
        new = go_notes(go)
        if not new:
            print("  go   build %s: no commits in the window — leaving its notes untouched" % go["build"])
        else:
            i, j = region(page, "go")
            new += "\n"
            if page[i:j] != new:
                behind.append("go")
                page = page[:i] + new + page[j:]
            print("  go   %s, %d change(s) since %s%s" % (
                go["label"], go["total"], go["since"], "" if "go" in behind else "  (unchanged)"))
    gv = go_version_info()
    if gv is None:
        print("  go   version: go.flyconcordefly.com/version did not answer — leaving the line as it is")
    else:
        new = stamp_go_version(page, gv)
        if new != page:
            behind.append("go version")
            page = new
        print("  go   version %s, updated %s%s" % (gv["version"], gv["updated"],
                                                 "" if "go version" in behind else "  (unchanged)"))
    page, relabelled = stamp_since(page)
    n = sum(1 for m in SINCE.finditer(page) if page.rfind("<!--", 0, m.start()) <= page.rfind("-->", 0, m.start()))
    if n:
        print("  faq  %d version label(s)%s" % (n, "" if relabelled else "  (unchanged)"))
    if relabelled:
        behind.append("faq labels")
    # Generation replaces the whole region, so the result cannot repeat a
    # channel; if it somehow does, that is a bug and the page must not ship.
    for key, _repo, _buttons in PRODUCTS:
        still = repeats(page, key)
        if still:
            sys.exit("index.html: %s block still repeats %s after generating"
                     % (key, ", ".join(still)))
    # …and nothing pretending to be a channel outside the generated regions:
    # a block pasted one line past a close marker would otherwise show on the
    # page twice with every check reporting the page as current.
    problem = page_problem(page)
    if problem:
        sys.exit(problem)
    if check:
        if behind:
            sys.exit("index.html is behind for: %s — run tools/sync-releases.py" % ", ".join(behind))
        print("  index.html is current")
        return
    if behind:
        INDEX.write_text(page, encoding="utf-8")
        print("  wrote index.html (%s)" % ", ".join(behind))


if __name__ == "__main__":
    main()
