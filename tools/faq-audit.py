#!/usr/bin/env python3
"""Check the site's FAQ against each new ConcordeAI / ConcordeVPN release, and
open a GitHub issue on this repo when an answer looks out of date.

Runs in the Sync releases Action after the summarizer, and only there: it
needs the ANTHROPIC_API_KEY secret, the anthropic SDK and `gh` with a token
that may open issues. Every stable and prerelease (never a nightly) is
checked once; tools/faq-audit.json records which were, and is committed with
the page. A failure of any kind leaves that release unchecked for the next
run and never stops the sync. Pat asked for this on 2026-09-28, so a changed
feature cannot leave the FAQ describing the old one unnoticed.

    python3 tools/faq-audit.py --stash FILE   # the Action: check, record, file issues
    python3 tools/faq-audit.py --offline      # merge the stash only (a push retry)
    python3 tools/faq-audit.py --dry-run      # say what would be checked
    python3 tools/faq-audit.py --seed         # mark every current release checked, call nothing
"""
import argparse
import datetime
import html
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "faq-audit.json"
PROMPT = HERE / "faq-audit-prompt.md"
MAX_PER_RUN = 3                          # releases checked in one run; the rest wait for the next
DOC = ("Releases whose notes have been checked against the FAQ, as repo@tag. Written by "
       "tools/faq-audit.py in the Sync releases Action. Delete an entry to have it checked again.")
SCHEMA = {
    "type": "object",
    "properties": {
        "stale": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "problem": {"type": "string"},
                "suggestion": {"type": "string"},
            },
            "required": ["question", "problem", "suggestion"],
            "additionalProperties": False,
        }},
    },
    "required": ["stale"],
    "additionalProperties": False,
}
NAMES = {"ai": "ConcordeAI", "vpn": "ConcordeVPN"}


def load_sync():
    spec = importlib.util.spec_from_file_location("sync_releases", HERE / "sync-releases.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_state(path):
    try:
        got = json.loads(Path(path).read_text(encoding="utf-8")).get("audited")
        return set(got) if isinstance(got, list) else set()
    except (OSError, ValueError, AttributeError):
        return set()


def write_state(path, audited):
    Path(path).write_text(json.dumps({"_": DOC, "audited": sorted(audited)}, indent=1) + "\n", encoding="utf-8")


def faq_text(page):
    """The FAQ as plain text: each product's questions and answers."""
    a, b = page.index('id="faq"'), page.index('<!-- ============ CONTACT')
    out = []
    for group in re.split(r'<div class="faq-group">', page[a:b])[1:]:
        name = html.unescape(re.sub(r"<[^>]+>", "", re.search(r"<h3>(.*?)</h3>", group, re.S).group(1)))
        out.append("## " + " ".join(name.split())[:40])
        for q, ans in re.findall(r"<summary>(.*?)</summary><div class=\"a\">(.*?)</div></details>", group, re.S):
            out.append("Q: " + html.unescape(re.sub(r"<[^>]+>", "", q)))
            out.append("A: " + " ".join(html.unescape(re.sub(r"<[^>]+>", "", ans)).split()))
    return "\n".join(out)


def pending(sync, audited):
    """[(key, repo, rel)] not yet checked, oldest first, from the last 30 days."""
    out = []
    for key, repo, _buttons in sync.PRODUCTS:
        for _date, _title, rels in sync.news_groups(repo):
            for rel in rels:
                if "%s@%s" % (repo, rel["tag_name"]) not in audited:
                    out.append((key, repo, rel))
    out.sort(key=lambda t: t[2]["published_at"])
    return out


def ask(client, sync, system, faq, key, title, notes):
    import anthropic
    user = ("Release: %s %s\n\n<release_notes>\n%s\n</release_notes>\n\n<faq>\n%s\n</faq>"
            % (NAMES[key], title, "\n".join("- " + n for n in notes), faq))
    try:
        r = client.beta.messages.create(
            model=sync.MODEL, max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            thinking={"type": "adaptive"}, system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
    except anthropic.APIError as exc:
        raise RuntimeError("API: %s" % exc) from exc
    if r.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError("stopped: %s (request %s)" % (r.stop_reason, getattr(r, "_request_id", None)))
    text = next((b.text for b in r.content if getattr(b, "type", "") == "text"), None)
    data = json.loads(text or "")
    stale = [s for s in data.get("stale", []) if isinstance(s, dict) and s.get("question")]
    return stale[:10]


def file_issue(title, body):
    """Open the issue unless one with this title already exists."""
    have = subprocess.run(["gh", "issue", "list", "--state", "all", "--search", title + " in:title",
                           "--json", "title", "--limit", "20"], capture_output=True, text=True)
    if have.returncode == 0 and any(i.get("title") == title for i in json.loads(have.stdout or "[]")):
        print("    issue already open: %s" % title)
        return True
    made = subprocess.run(["gh", "issue", "create", "--title", title, "--body", body],
                          capture_output=True, text=True)
    if made.returncode:
        print("    could not open the issue: %s" % made.stderr.strip()[:200])
        return False
    print("    opened %s" % made.stdout.strip())
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stash", help="file outside the checkout that keeps this run's record across resets")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true")
    args = ap.parse_args(argv)
    sync = load_sync()
    audited = read_state(STATE) | (read_state(args.stash) if args.stash else set())
    try:
        todo = pending(sync, audited)
    except (sync.Incomplete, SystemExit) as exc:
        print("  faq  releases unreadable (%s) — nothing checked" % exc)
        return 0
    if args.seed:
        audited |= {"%s@%s" % (repo, rel["tag_name"]) for _k, repo, rel in todo}
        write_state(STATE, audited)
        print("  faq  marked %d release(s) as checked" % len(todo))
        return 0
    if args.dry_run or args.offline or not todo:
        for key, _repo, rel in todo:
            print("  faq  %s %s — %s" % (NAMES[key], sync.release_title(rel), "would check" if args.dry_run else "waits"))
        if args.offline and args.stash:
            write_state(STATE, audited)
        if not todo:
            print("  faq  every release checked")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  faq  ANTHROPIC_API_KEY is not set — %d release(s) wait" % len(todo))
        return 0
    try:
        import anthropic
        client = anthropic.Anthropic(timeout=90, max_retries=1)
        system = PROMPT.read_text(encoding="utf-8")
        faq = faq_text(sync.INDEX.read_text(encoding="utf-8"))
    except Exception as exc:
        print("  faq  not set up (%s) — nothing checked" % type(exc).__name__)
        return 0
    for key, repo, rel in todo[:MAX_PER_RUN]:
        title = sync.release_title(rel)
        notes = [i["text"] for i in sync.source_items(repo, [rel])]
        tag = "%s@%s" % (repo, rel["tag_name"])
        if not notes:
            audited.add(tag)
            continue
        print("  faq  %s %s — checking %d note(s) against the FAQ" % (NAMES[key], title, len(notes)))
        try:
            stale = ask(client, sync, system, faq, key, title, notes)
        except Exception as exc:
            print("    not checked, will retry next run: %s" % str(exc)[:200])
            break
        if stale:
            body = ["%s %s may have changed what these FAQ answers on flyconcordefly.com should say. "
                    "Checked automatically by tools/faq-audit.py; close this if nothing needs to change.\n"
                    % (NAMES[key], title)]
            for s in stale:
                body.append("### %s\n**What looks out of date:** %s\n\n**Suggested:** %s\n"
                            % (s["question"], s["problem"], s["suggestion"]))
            body.append("Release: https://github.com/%s/releases/tag/%s" % (repo, rel["tag_name"]))
            if not file_issue("FAQ check: %s %s may affect %d answer%s"
                              % (NAMES[key], title, len(stale), "" if len(stale) == 1 else "s"), "\n".join(body)):
                break
        else:
            print("    the FAQ still holds")
        audited.add(tag)
        write_state(STATE, audited)
        if args.stash:
            write_state(args.stash, audited)
    if len(todo) > MAX_PER_RUN:
        print("  faq  %d release(s) left for the next run" % (len(todo) - MAX_PER_RUN))
    return 0


if __name__ == "__main__":
    sys.exit(main())
