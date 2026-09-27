#!/usr/bin/env python3
"""Summarize each download channel's release notes with Claude, once.

Runs in the GitHub Action only (it is the one place with the API key and
the `anthropic` package); the release scripts never call it. For every
channel the page shows — stable, prerelease, nightly, per product — it
takes the same source notes sync-releases.py would use, and when
tools/release-notes.auto.json has no summary for exactly those notes it
asks Claude for one and stores it there. sync-releases.py then shows it.

    python3 tools/summarize-notes.py --stash "$RUNNER_TEMP/notes.json"
    python3 tools/summarize-notes.py --stash "$RUNNER_TEMP/notes.json" --offline
                                                   # merge paid-for results only, call nothing
    python3 tools/summarize-notes.py --dry-run     # say what would be asked, call nothing, write nothing

A summary is keyed by a hash of its source notes, the prompt file and the
model, so each distinct set of notes costs one call, ever. A summary can
be edited by hand in the JSON and stays until the notes change. Entries
no channel refers to any more are dropped.

--stash is a file outside the checkout that every new result is appended
to the moment it arrives. The workflow resets the checkout to origin/main
before each push attempt; the stash is merged back in first, so a retry
never pays for the same summary twice.

Failures never block the sync: the channel keeps its heuristic notes. A
refusal, a truncated or unusable answer is recorded as "failed" so it is
not paid for again (for FAILED_RETRY_DAYS, then asked once more); a
network, rate-limit or server error is not recorded, so the next run tries
again. The first network, rate-limit, server or key error ends the run —
the rest would fail the same way — and no call is started after
TIME_BUDGET seconds, so a slow API cannot hold up the sync behind it.

It writes nothing and calls nothing when tools/release-notes.auto.json does
not parse (a hand edit with a typo: fix it, nothing is lost) or when
index.html is in a state the sync would refuse to write (nothing paid for
could ship).
"""
import argparse
import datetime
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTO_DOC = ("Channel summaries, generated. Keyed by repo, then a hash of the channel's source notes + "
            "tools/release-notes-prompt.md + the model (see channel_key in sync-releases.py). Written by "
            "tools/summarize-notes.py in the GitHub Action (model = the Claude model that wrote it) or by "
            "hand (model \"hand\"). Edit \"bullets\" freely: an entry is kept until its channel's notes "
            "change. \"fold\": true adds the closing \"Plus smaller fixes and polish\" line. An entry with "
            "\"failed\" is ignored by the page and asked again after 7 days; delete it to try sooner. "
            "Keep this file valid JSON: while it does not parse, nothing is summarized.")

PRODUCT = {
    "bigmillz/concordeai": "ConcordeAI, a desktop AI assistant for Mac and Windows that runs models on the "
                           "user's own computer, with cloud models as an option",
    "bigmillz/concordevpn-releases": "ConcordeVPN, a Mac VPN app that runs through the user's own server",
}
SPAN = {
    "stable": "These notes cover every build since the previous stable release.",
    "prerelease": "These notes cover every beta and release candidate since the last stable release.",
    "nightly": "These notes cover this nightly build: what is being worked on right now.",
}

# The answer's shape. "fold": there were smaller changes not listed, and the
# page adds its own closing line (with the link) for them.
SCHEMA = {
    "type": "object",
    "properties": {
        "bullets": {"type": "array", "items": {"type": "string"}},
        "fold": {"type": "boolean"},
    },
    "required": ["bullets", "fold"],
    "additionalProperties": False,
}
PRICE_IN, PRICE_OUT = 5.0, 25.0          # Claude Opus 5, $ per million tokens
TIMEOUT, RETRIES = 90, 1                 # per request; the SDK's defaults (600 s, 2) could stall a run for half an hour
TIME_BUDGET = 180                        # seconds: no new call is started after this (the workflow kills at 480)
FAILED_RETRY_DAYS = 7                    # a recorded failure is asked again after this long


class Transient(Exception):
    """Try again next run (network, rate limit, server, bad key)."""


class StopAll(Transient):
    """Transient, and every other call this run would fail the same way."""


class Unusable(Exception):
    """This answer can't be used and asking again won't change that."""


def load_sync():
    spec = importlib.util.spec_from_file_location("sync_releases", HERE / "sync-releases.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_cache(path):
    """(cache, None), or (None, why) when the file exists and is not a JSON
    object — a hand edit gone wrong, which must be fixed, not overwritten."""
    path = Path(path)
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return None, str(exc)
    except OSError as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "the top level is not an object"
    return data, None


def expired_failure(entry, today):
    """A recorded failure old enough to be asked about once more."""
    if not (isinstance(entry, dict) and entry.get("failed")):
        return False
    try:
        then = datetime.date.fromisoformat(str(entry.get("generated")))
        now = datetime.date.fromisoformat(today)
    except ValueError:
        return True
    return (now - then).days >= FAILED_RETRY_DAYS


def write_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def user_message(repo, kind, label, items):
    """The notes, as data. Tags let a bullet be traced to its build; nothing
    in here is an instruction, and the system prompt says so."""
    lines = []
    for it in items:
        text = re.sub(r"<\s*/?\s*source_notes\b[^>]*>", "", it["text"], flags=re.I)
        mark = " (the stable's own notes: may retell the betas)" if it.get("restates") else ""
        lines.append("- [%s%s] %s" % (it["tag"], mark, text))
    return ("Product: %s\nChannel: %s (%s). %s\n\n<source_notes>\n%s\n</source_notes>"
            % (PRODUCT.get(repo, repo), kind, label, SPAN[kind], "\n".join(lines)))


def tidy(data, cap):
    """The model's JSON checked and normalized, or Unusable."""
    if not isinstance(data, dict) or not isinstance(data.get("bullets"), list) \
            or not isinstance(data.get("fold"), bool):
        raise Unusable("answer is not {bullets: [...], fold: bool}")
    fold, out = data["fold"], []
    for b in data["bullets"]:
        if not isinstance(b, str):
            raise Unusable("a bullet is not a string")
        b = " ".join(b.split())
        b = re.sub(r"^([-*•]\s+|\d+[.)]\s+)", "", b).rstrip(" .")
        if not b:
            continue
        if re.match(r"^(plus|and)\b.*\b(more|smaller|minor|other|fix\w*|polish)\b", b, re.I):
            fold = True                  # it wrote the closing line itself; the page adds that
            continue
        # a public page: no links, domains, script URLs, Markdown links, code
        # spans or HTML — only **bold** is rendered, everything else shows raw
        if re.search(r"https?://|www\.|javascript:|<[a-z/!]|\[[^\]]*\]\(|`|"
                     r"\b[a-z0-9-]+\.(com|net|org|io|app|dev|ai|co|me|sh|xyz)\b", b, re.I):
            raise Unusable("a bullet carries a link or markup: %r" % b[:80])
        if len(b) > 220:
            raise Unusable("a bullet runs to %d characters" % len(b))
        out.append(b)
    if not out and not fold:
        raise Unusable("no bullets")
    return out[:cap - (1 if fold else 0)], fold


def ask(client, sync, system, repo, kind, label, items):
    """One summary: (bullets, fold, model that answered). Raises Transient
    or Unusable; the chain runs most specific first."""
    import anthropic
    try:
        response = client.beta.messages.create(
            model=sync.MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                 # a refusal is re-run on Anthropic's recommended fallback
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user_message(repo, kind, label, items)}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
    except anthropic.AuthenticationError as exc:
        raise StopAll("the API key was rejected (request %s)" % getattr(exc, "request_id", None)) from exc
    except anthropic.RateLimitError as exc:
        raise StopAll("rate limited (request %s)" % getattr(exc, "request_id", None)) from exc
    except anthropic.BadRequestError as exc:
        # our request is wrong (or the SDK is too old for it): not the notes' fault, and a 400 is not billed
        raise Transient("bad request: %s (request %s)" % (exc.message, getattr(exc, "request_id", None))) from exc
    except anthropic.APIStatusError as exc:
        rid = getattr(exc, "request_id", None)
        if exc.status_code >= 500:           # overloaded or down: the next channel would wait just as long
            raise StopAll("API error %s (request %s)" % (exc.status_code, rid)) from exc
        raise Transient("API error %s (request %s)" % (exc.status_code, rid)) from exc
    except anthropic.APIConnectionError as exc:  # includes APITimeoutError
        raise StopAll("could not reach the API: %s" % exc) from exc

    rid = getattr(response, "_request_id", None)
    usage = getattr(response, "usage", None)
    if usage is not None:
        cost = (usage.input_tokens * PRICE_IN + usage.output_tokens * PRICE_OUT) / 1e6
        print("    %d in / %d out tokens, about $%.3f (request %s)"
              % (usage.input_tokens, usage.output_tokens, cost, rid))
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        retry_on = getattr(details, "recommended_model", None)
        if retry_on:
            # the fallback model was busy, so the fallback never ran: a
            # refusal by circumstance, not a verdict on the notes
            raise Transient("refused, and the fallback (%s) could not run (request %s)" % (retry_on, rid))
        raise Unusable("refused (%s, request %s)" % (getattr(details, "category", None), rid))
    if response.stop_reason == "max_tokens":
        raise Unusable("ran out of tokens (request %s)" % rid)
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
    if text is None:
        raise Unusable("no text in the answer (request %s)" % rid)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Unusable("answer is not JSON (request %s)" % rid) from exc
    bullets, fold = tidy(data, sync.SHOW_MAX)
    return bullets, fold, getattr(response, "model", None) or sync.MODEL


def main(argv=None, client=None, sync=None, today=None, clock=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stash", help="file outside the checkout that keeps paid-for results across resets")
    ap.add_argument("--dry-run", action="store_true", help="say what would be summarized; call nothing, write nothing")
    ap.add_argument("--offline", action="store_true", help="merge the stash only; call nothing")
    args = ap.parse_args(argv)
    sync = sync or load_sync()
    today = today or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    clock = clock or time.monotonic
    started = clock()

    cache, broken = read_cache(sync.AUTO)
    if broken:
        # never "repair" a hand edit by paying to regenerate everything over it
        print("::error::%s does not parse (%s) — fix it; nothing summarized, nothing written"
              % (sync.AUTO.name, broken))
        return 0
    problem = sync.page_problem()
    if problem:
        print("  %s — the sync cannot write the page, so nothing is summarized" % problem)
        return 0
    stash = read_json(args.stash) if args.stash else {}
    try:
        system = sync.PROMPT.read_text(encoding="utf-8")
    except OSError:
        print("  no %s — nothing to summarize with" % sync.PROMPT.name)
        return 0
    curated = sync.load_curated()

    todo, referenced, complete = [], {}, set()
    for key, repo, _buttons in sync.PRODUCTS:
        try:
            specs = sync.channel_specs(key, repo)
        except (sync.Incomplete, SystemExit) as exc:
            # a nightly being re-cut, or GitHub unreachable: leave this repo's entries alone
            print("  %-4s %s — skipped, its summaries kept" % (key, exc))
            continue
        complete.add(repo)
        keep = referenced.setdefault(repo, set())
        lines = (curated.get("lines") or {}).get(repo) or {}
        for name, rel, tag, line in specs:
            kind = name.lower()
            version = sync.display_version(sync.release_version(rel))
            label = " ".join(x for x in (version, tag) if x) if kind != "nightly" else rel.get("name") or "nightly"
            if kind != "nightly" and lines.get(version):
                print("  %-4s %-10s %s — hand-written line summary, not summarized" % (key, kind, label))
                continue
            items = sync.source_items(repo, line or [rel], curated)
            if not items:
                continue
            ck = sync.channel_key(kind, items)
            keep.add(ck)
            if ck in (cache.get(repo) or {}) and not expired_failure(cache[repo][ck], today):
                print("  %-4s %-10s %s — cached (%s)" % (key, kind, label, cache[repo][ck].get("model")))
            elif ck in (stash.get(repo) or {}) and not expired_failure(stash[repo][ck], today):
                cache.setdefault(repo, {})[ck] = stash[repo][ck]
                print("  %-4s %-10s %s — from this run's stash" % (key, kind, label))
            else:
                todo.append((key, repo, kind, label, items, ck, sorted({i["tag"] for i in items})))

    if args.dry_run:
        for key, repo, kind, label, items, ck, _tags in todo:
            print("  %-4s %-10s %s — would summarize %d notes (key %s)" % (key, kind, label, len(items), ck))
        return 0

    if todo and args.offline:
        print("  offline: %d channel(s) left for the next run" % len(todo))
        todo = []
    if todo and client is None:
        try:
            import anthropic
        except ImportError:
            print("  the anthropic package is not installed — %d channel(s) keep the heuristic notes" % len(todo))
            todo = []
        else:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                print("  ANTHROPIC_API_KEY is not set — %d channel(s) keep the heuristic notes" % len(todo))
                todo = []
            else:
                try:
                    client = anthropic.Anthropic(timeout=TIMEOUT, max_retries=RETRIES)
                except Exception as exc:     # never block the sync
                    print("  could not set up the API client (%s) — heuristic notes stand" % type(exc).__name__)
                    todo = []

    for n, (key, repo, kind, label, items, ck, tags) in enumerate(todo):
        if clock() - started > TIME_BUDGET:
            print("  out of time: %d channel(s) left for the next run" % (len(todo) - n))
            break
        print("  %-4s %-10s %s — summarizing %d notes" % (key, kind, label, len(items)))
        entry = {"channel": "%s %s" % (kind, label), "source_tags": tags, "generated": today}
        try:
            bullets, fold, model = ask(client, sync, system, repo, kind, label, items)
            entry.update(bullets=bullets, fold=fold, model=model)
            for b in bullets:
                print("      - %s" % b)
            if fold:
                print("      ~ Plus smaller fixes and polish")
        except Transient as exc:
            print("    not summarized, will retry next run: %s" % exc)
            if isinstance(exc, StopAll):
                break
            continue
        except Unusable as exc:
            print("    unusable, recorded so it is not paid for again: %s" % exc)
            entry.update(bullets=[], fold=False, model=sync.MODEL, failed=str(exc)[:200])
        except Exception as exc:             # an SDK too old for these parameters, say — never block the sync
            print("    not summarized (%s: %s), will retry next run" % (type(exc).__name__, exc))
            continue
        cache.setdefault(repo, {})[ck] = entry
        write_json(sync.AUTO, cache)         # paid for: on disk now, even if the run is killed
        if args.stash:                       # …and kept through the workflow's reset
            stash.setdefault(repo, {})[ck] = entry
            write_json(args.stash, stash)

    # Drop what no channel shows any more — but only for a repo we saw whole.
    out = {"_": AUTO_DOC}
    for repo, entries in cache.items():
        if repo == "_" or not isinstance(entries, dict):
            continue
        if repo in complete:
            gone = sorted(set(entries) - referenced.get(repo, set()))
            for ck in gone:
                print("  pruned %s %s (%s)" % (repo, ck, entries[ck].get("channel", "")))
            entries = {ck: e for ck, e in entries.items() if ck not in gone}
        if entries:
            out[repo] = entries
    try:
        was = sync.AUTO.read_text(encoding="utf-8")
    except OSError:
        was = None
    if was != json.dumps(out, ensure_ascii=False, indent=2) + "\n":
        write_json(sync.AUTO, out)
        print("  wrote %s" % sync.AUTO.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
