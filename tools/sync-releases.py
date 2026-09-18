#!/usr/bin/env python3
"""Write the download blocks in index.html from GitHub Releases.

The site is static and the release data used to be typed in by hand, which
is how it came to advertise a five-week-old stable and an RC that no longer
existed. Now the blocks between the `<!-- releases:KEY -->` markers are
generated, and the page can only be behind until someone runs this.

    python3 tools/sync-releases.py            # rewrite index.html, say what changed
    python3 tools/sync-releases.py --check    # exit 1 if the page is behind (CI)

Needs the `gh` CLI (authenticated, or unauthenticated for the public repos).

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
Dates are the release's publish time, UTC, same as the footer. Each channel
gets a "Release notes" dropdown: a few visitor-facing bullets from
release-notes.json when we have written them, otherwise a condensation of
the GitHub body (its bullets or first sentence, install boilerplate dropped).
Notes cover everything a channel stands for: a prerelease covers every
beta/RC cut since the last stable, a stable covers everything since the
previous stable — so the list grows as a line matures. Past NOTES_MAX
bullets each is cut to its first clause and the remainder is linked, and
a hand-written "lines" entry replaces the accumulation outright.
"""
import html
import json
import re
import subprocess
import sys
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


def gh(path):
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if out.returncode:
        sys.exit("gh api %s failed: %s" % (path, out.stderr.strip()))
    return json.loads(out.stdout)


NIGHTLY_TAG = "nightly"          # one rolling release per repo; its title carries the commit

# Products whose Prerelease (beta/RC) block is SUPPRESSED on the site, however
# many prereleases exist upstream. Pat, 2026-09-18: 1.3 went stable and the 1.4
# line ships as nightlies only — no beta block until he says "cut 1.4 beta".
# TO SHOW IT AGAIN: take the key out of this set. Nothing else to change.
# Nightly and Stable blocks are unaffected.
HIDE_PRERELEASE = {"vpn"}


def is_nightly(rel):
    return rel["tag_name"] == NIGHTLY_TAG or "nightly" in (rel.get("name") or "").lower()


def channels(repo):
    """stable, beta (or RC), nightly, and the release lines each stands for:
    beta_line is every prerelease since stable, stable_line is stable plus
    every prerelease that led to it. A beta counts only while it is newer
    than stable, so that block disappears the moment stable overtakes it;
    the nightly is shown whenever one exists."""
    rels = [r for r in gh("repos/%s/releases?per_page=100" % repo) if not r["draft"]]
    rels.sort(key=lambda r: r["published_at"], reverse=True)
    stables = [r for r in rels if not r["prerelease"]]
    if not stables:
        sys.exit("%s has no stable release" % repo)
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


def inline(t):
    """Escape, then allow **bold** and `code` only."""
    t = html.escape(t, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    return t


BOILERPLATE = re.compile(r"^\*\*(macos|windows|linux)\*\*|^verify\b|^bring-your-own|^shasum ", re.I)


def condense(md, limit=4):
    """A release body reduced to what a visitor would want: its bullet
    items, or failing that the first sentence of the first paragraph —
    each cut back to its first sentence or clause. Install instructions
    and sign-offs are dropped."""
    md = md.replace("\r\n", "\n")
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
        if BOILERPLATE.match(it):
            continue
        it = re.sub(r"^\*\*[^*]+\*\*\s*$", "", it).strip()          # a bold-only line is a title, not a note
        if not it:
            continue
        it = re.split(r"(?<=[.!?])\s+", it)[0]                       # first sentence
        if len(it) > 120:                                           # then its first clause,
            head = re.split(r"\s[:;]\s", it)[0]                     # unless that is just a bold lead
            it = head if len(re.sub(r"\W", "", head)) > 24 else it
        if len(it) > 140:
            it = it[:137].rsplit(" ", 1)[0] + "\u2026"
        if it.count("(") > it.count(")"):                           # never end inside a parenthesis
            it = it[:it.rfind("(")].rstrip(" ,;:\u2026") + "\u2026"
        out.append(it.rstrip(".").strip())
        if len(out) == limit:
            break
    return out


def load_curated():
    return json.loads(CURATED.read_text(encoding="utf-8")) if CURATED.exists() else {}


def tighten(item):
    """A bullet cut back to its first clause — applied once a list is long
    enough that every line has to earn its width. The clause is only taken
    when what is left still says something (a bold lead alone does not)."""
    t = re.sub(r"\s*\([^)]*\)", "", item).strip()
    if len(t) > 90:
        for sep in (r"\s[\u2014\u2013]\s", r":\s", r";\s"):
            head = re.split(sep, t, maxsplit=1)[0]
            if len(head) < len(t) and len(re.sub(r"\W", "", head)) > 28:
                t = head
                break
    if len(t) > 120:
        t = t[:117].rsplit(" ", 1)[0] + "\u2026"
    return t.rstrip(" .,;:\u2014\u2013").strip()


def notes_items(repo, rel, curated):
    ours = (curated.get(repo) or {})
    if rel["tag_name"] in ours:            # an entry — even an empty one — is the last word
        return ours[rel["tag_name"]]
    return condense(rel.get("body") or "")


NOTES_MAX = 8            # bullets before the list compresses


def notes_for(repo, rels, version="", limit=NOTES_MAX):
    """One bullet list for a channel, covering every release it stands for:
    a prerelease covers its line since the last stable, a stable covers
    everything since the previous stable. So the list grows as a line
    matures — and because that would end up absurd by the time a stable
    lands, past the cap every bullet is cut to its first clause and the
    remainder is named and linked rather than silently dropped.

    A hand-written summary for the whole line (release-notes.json ->
    "lines" -> repo -> version) replaces the accumulation outright; that is
    the lever to pull when a line has too much history to list. It applies
    only when `version` is given, i.e. to a channel standing for a line —
    never to a nightly, which is one build and says what that build did."""
    curated = load_curated()
    summary = ((curated.get("lines") or {}).get(repo) or {}).get(version)
    if summary:
        return "<ul>%s</ul>" % "".join("<li>%s</li>" % inline(i) for i in summary)

    items, seen = [], set()
    for rel in rels:
        for it in notes_items(repo, rel, curated):
            k = re.sub(r"\W+", " ", it).strip().lower()
            if k and k not in seen:
                seen.add(k)
                items.append(it)
    if not items:
        return ""

    extra = 0
    if len(items) > limit:
        extra = len(items) - limit
        items = [tighten(i) for i in items[:limit]]
    lis = ["<li>%s</li>" % inline(i) for i in items]
    if extra:
        # the whole releases list, not one tag: a channel's notes span
        # several releases, and no single tag page shows all of them
        url = "https://github.com/%s/releases" % repo
        more = "and %d more change%s" % (extra, "" if extra == 1 else "s")
        lis.append('<li class="more"><a href="%s">%s, on GitHub</a></li>'
                   % (html.escape(url), more))
    return "<ul>%s</ul>" % "".join(lis)


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
        sys.exit("%s %s has none of the expected assets" % (name, rel["tag_name"]))
    m = re.search(r"(\d+\.\d+\.\d+)", picks[0][1]["name"])
    version = m.group(1) if m else rel["name"]
    date = (stamp(rel) if is_nightly(rel) else rel["published_at"])[:10]
    sha = next((a for a in rel["assets"] if a["name"].endswith(".sha256")), None)

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
        lines.append('                <span class="chan-commit" data-nightly-commit="%s"%s>%s</span>'
                     % (key, ' data-commit-base="%s"' % base if base else "", commit or "&mdash;"))
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
    notes = notes_for(repo, notes_rels or [rel],
                      display_version(version) if notes_rels else "")
    if notes:
        lines += ['              <details class="sum notes">',
                  '                <summary>Release notes</summary>',
                  '                <div class="sum-body">',
                  notes,
                  '                </div>',
                  '              </details>']
    if sha:
        lines += ['              <details class="sum">',
                  '                <summary>Verify checksum</summary>',
                  '                <div class="sum-body">',
                  '<code>shasum -a 256 -c %s</code>' % html.escape(sha["name"]),
                  '                  <a href="%s">%s</a>' % (html.escape(sha["browser_download_url"]),
                                                            html.escape(sha["name"])),
                  '                </div>',
                  '              </details>']
    lines.append('            </div>')
    return "\n".join(lines)


def block(key, repo, buttons):
    stable, beta, nightly, beta_line, stable_line = channels(repo)
    parts = [channel_html("Stable", stable, buttons, repo, notes_rels=stable_line)]
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
        parts.append(channel_html("Prerelease", beta, buttons, repo, tag, notes_rels=beta_line))
    if nightly:
        parts.append(channel_html("Nightly", nightly, buttons, repo))
    summary = "%s (%s)" % (stable["tag_name"], stable["name"])
    if beta:
        summary += " + %s (%s)" % (beta["tag_name"], beta["name"])
    if nightly:
        summary += " + nightly %s" % (commit_of(nightly) or nightly["tag_name"])
    return "\n\n".join(parts) + "\n", summary


def region(page, key):
    """The generated span for one product, as (start, end) offsets. Exactly
    one marker pair must exist: with two, there is no safe place to write."""
    begin, end = "            <!-- releases:%s -->\n" % key, "            <!-- /releases:%s -->" % key
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


def main():
    check = "--check" in sys.argv
    page = INDEX.read_text(encoding="utf-8")
    behind = []
    for key, repo, buttons in PRODUCTS:
        was = repeats(page, key)
        if was:
            print("  %-4s repeating %s — regenerating fixes it" % (key, ", ".join(was)))
        i, j = region(page, key)
        new, summary = block(key, repo, buttons)
        if page[i:j] != new:
            behind.append(key)
            page = page[:i] + new + page[j:]
        elif was:
            behind.append(key)
        print("  %-4s %s%s" % (key, summary, "" if key in behind else "  (unchanged)"))
    # Generation replaces the whole region, so the result cannot repeat a
    # channel; if it somehow does, that is a bug and the page must not ship.
    for key, _repo, _buttons in PRODUCTS:
        still = repeats(page, key)
        if still:
            sys.exit("index.html: %s block still repeats %s after generating"
                     % (key, ", ".join(still)))
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
