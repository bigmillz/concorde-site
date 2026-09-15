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
  RC/Beta   the newest prerelease, only while it is newer than stable —
            once stable overtakes it the second channel disappears
The version is read from the asset filename (`ConcordeAI-6.0.0.dmg` -> 6.0.0)
because that is the string people see on disk; release *names* are labels
and have drifted from the files before. Build numbers are not shown.
Dates are the release's publish time, UTC, same as the footer. Each channel
gets a "Release notes" dropdown rendered from the release body.
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


def channels(repo):
    rels = [r for r in gh("repos/%s/releases?per_page=40" % repo) if not r["draft"]]
    stable = next((r for r in rels if not r["prerelease"]), None)
    if stable is None:
        sys.exit("%s has no stable release" % repo)
    pre = next((r for r in rels if r["prerelease"]
                and r["published_at"] > stable["published_at"]), None)
    return stable, pre


def md_to_html(md):
    """The subset of GitHub markdown the release notes use — paragraphs,
    **bold**, `code`, bullet lists, indented or fenced code blocks, headings
    (shown as bold lines), and plain http(s) links. Everything is escaped
    first; anything unrecognised is shown as text, never as markup."""
    def inline(t):
        t = html.escape(t, quote=False)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
        return t
    out, para, items, code, fenced = [], [], [], [], False
    def flush():
        if para:
            out.append("<p>%s</p>" % inline(" ".join(para))); para.clear()
        if items:
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % inline(i) for i in items)); items.clear()
        if code:
            out.append("<pre>%s</pre>" % html.escape("\n".join(code), quote=False)); code.clear()
    for ln in md.replace("\r\n", "\n").split("\n"):
        if ln.strip().startswith("```"):
            if not fenced: flush()
            fenced = not fenced
            if not fenced: flush()
            continue
        if fenced or ln.startswith("    ") or ln.startswith("\t"):
            if not code: flush()
            code.append(ln if fenced else ln[4:] if ln.startswith("    ") else ln[1:])
            continue
        if code: flush()
        st = ln.strip()
        if not st:
            flush(); continue
        m = re.match(r"^[-*] +(.*)", st)
        if m:
            if para: flush()
            items.append(m.group(1)); continue
        m = re.match(r"^#{1,6} +(.*)", st)
        if m:
            flush(); out.append("<p><b>%s</b></p>" % inline(m.group(1))); continue
        if items: flush()
        para.append(st)
    flush()
    return "\n".join(out)


def pretty(iso):
    y, m, d = iso[:10].split("-")
    return "%d %s %s" % (int(d), MONTHS[int(m) - 1], y)


def channel_html(name, rel, buttons):
    picks = []
    for label, pat in buttons:
        asset = next((a for a in rel["assets"] if re.search(pat, a["name"])), None)
        if asset:
            picks.append((label, asset))
    if not picks:
        sys.exit("%s %s has none of the expected assets" % (name, rel["tag_name"]))
    m = re.search(r"(\d+\.\d+\.\d+)", picks[0][1]["name"])
    version = m.group(1) if m else rel["name"]
    date = rel["published_at"][:10]
    sha = next((a for a in rel["assets"] if a["name"].endswith(".sha256")), None)

    lines = ['            <div class="chan">',
             '              <div class="chan-top">',
             '                <span class="chan-name">%s</span>' % name,
             '                <span class="chan-ver">%s</span>' % html.escape(version),
             '                <span class="chan-date">Released <time datetime="%s">%s</time></span>'
             % (date, pretty(date)),
             '              </div>',
             '              <div class="btns">']
    for label, asset in picks:
        lines += ['                <a class="btn" href="%s">' % html.escape(asset["browser_download_url"]),
                  '                  <span class="os">%s</span>' % label,
                  '                  <span class="file">%s</span>' % html.escape(asset["name"]),
                  '                  ' + ARROW,
                  '                </a>']
    lines.append('              </div>')
    notes = md_to_html((rel.get("body") or "").strip())
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
    stable, pre = channels(repo)
    parts = [channel_html("Stable", stable, buttons)]
    if pre:
        kind = ('<abbr title="Release candidate">RC</abbr>'
                if re.search(r"\bRC\b", pre["name"] or "", re.I) else "Beta")
        parts.append(channel_html(kind, pre, buttons))
    summary = "%s (%s)" % (stable["tag_name"], stable["name"])
    if pre:
        summary += " + prerelease %s (%s)" % (pre["tag_name"], pre["name"])
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
