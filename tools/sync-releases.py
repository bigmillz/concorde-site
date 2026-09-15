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
            newer than stable — once stable overtakes it, it disappears
  Nightly   the one rolling release tagged `nightly` (title "1.3 nightly
            <commit>"), only while newer than beta and stable; shown with
            the commit it was built from and a "Built" date taken from
            when its files last changed
The version is read from the asset filename (`ConcordeAI-6.0.0.dmg` -> 6.0.0)
because that is the string people see on disk; release *names* are labels
and have drifted from the files before. Build numbers are not shown.
Dates are the release's publish time, UTC, same as the footer. Each channel
gets a "Release notes" dropdown: a few visitor-facing bullets from
release-notes.json when we have written them, otherwise a condensation of
the GitHub body (its bullets or first sentence, install boilerplate dropped).
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


def is_nightly(rel):
    return rel["tag_name"] == NIGHTLY_TAG or "nightly" in (rel.get("name") or "").lower()


def channels(repo):
    """stable, beta (or RC), nightly — each None when absent. A prerelease
    counts only while it is newer than what is above it, so a channel
    disappears the moment stable (or beta) overtakes it."""
    rels = [r for r in gh("repos/%s/releases?per_page=40" % repo) if not r["draft"]]
    stable = next((r for r in rels if not r["prerelease"]), None)
    if stable is None:
        sys.exit("%s has no stable release" % repo)
    beta = next((r for r in rels if r["prerelease"] and not is_nightly(r)
                 and r["published_at"] > stable["published_at"]), None)
    floor = (beta or stable)["published_at"]
    # the rolling tag first; a numbered nightly only as long as no rolling one exists
    nightly = next((r for r in rels if r["tag_name"] == NIGHTLY_TAG), None) \
        or next((r for r in rels if r["prerelease"] and is_nightly(r)), None)
    if nightly and stamp(nightly) <= floor:
        nightly = None
    return stable, beta, nightly


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


def notes_for(repo, rel):
    curated = {}
    if CURATED.exists():
        curated = json.loads(CURATED.read_text(encoding="utf-8"))
    items = (curated.get(repo) or {}).get(rel["tag_name"]) or condense(rel.get("body") or "")
    if not items:
        return ""
    return "<ul>%s</ul>" % "".join("<li>%s</li>" % inline(i) for i in items)


def pretty(iso):
    y, m, d = iso[:10].split("-")
    return "%d %s %s" % (int(d), MONTHS[int(m) - 1], y)


def channel_html(name, rel, buttons, repo):
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
             '                <span class="chan-ver">%s</span>' % html.escape(version)]
    if is_nightly(rel):
        commit = commit_of(rel)
        if commit and not repo.endswith("-releases"):       # a public repo: link the commit
            commit = '<a href="https://github.com/%s/commit/%s">%s</a>' % (repo, commit, commit)
        lines.append('                <span class="chan-commit">%s</span>' % (commit or "&mdash;"))
    lines += ['                <span class="chan-date">%s <time datetime="%s">%s</time></span>'
              % ("Built" if is_nightly(rel) else "Released", date, pretty(date)),
              '              </div>']
    if is_nightly(rel):
        lines.append('              <p class="chan-note">Every landed change, as it lands. '
                     'Replaced by the next one; expect rough edges.</p>')
    lines += [
             '              <div class="btns">']
    for label, asset in picks:
        lines += ['                <a class="btn" href="%s">' % html.escape(asset["browser_download_url"]),
                  '                  <span class="os">%s</span>' % label,
                  '                  <span class="file">%s</span>' % html.escape(asset["name"]),
                  '                  ' + ARROW,
                  '                </a>']
    lines.append('              </div>')
    notes = notes_for(repo, rel)
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


def block(repo, buttons):
    stable, beta, nightly = channels(repo)
    parts = [channel_html("Stable", stable, buttons, repo)]
    if beta:
        kind = ('<abbr title="Release candidate">RC</abbr>'
                if re.search(r"\bRC\b", beta["name"] or "", re.I) else "Beta")
        parts.append(channel_html(kind, beta, buttons, repo))
    if nightly:
        parts.append(channel_html("Nightly", nightly, buttons, repo))
    summary = "%s (%s)" % (stable["tag_name"], stable["name"])
    if beta:
        summary += " + %s (%s)" % (beta["tag_name"], beta["name"])
    if nightly:
        summary += " + nightly %s" % (commit_of(nightly) or nightly["tag_name"])
    return "\n\n".join(parts) + "\n", summary


def main():
    check = "--check" in sys.argv
    page = INDEX.read_text(encoding="utf-8")
    behind = []
    for key, repo, buttons in PRODUCTS:
        begin, end = "            <!-- releases:%s -->\n" % key, "            <!-- /releases:%s -->" % key
        i = page.index(begin) + len(begin)
        j = page.index(end, i)
        new, summary = block(repo, buttons)
        if page[i:j] != new:
            behind.append(key)
            page = page[:i] + new + page[j:]
        print("  %-4s %s%s" % (key, summary, "" if key in behind else "  (unchanged)"))
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
