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

ConcordeGo is walked too: one "live" channel, the commits deployed in the
30 days up to ConcordeGo's newest build, from its public change list (sync.go_channel()),
cached under "concordego" — its own key, so pruning one never touches the
other. It is summarized once per Go deploy the Action sees.

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
AUTO_DOC = ("Channel summaries, generated. Keyed by repo (\"concordego\" for ConcordeGo's notes), then a "
            "hash of the channel's source notes + tools/release-notes-prompt.md + the model (see "
            "channel_key in sync-releases.py). Written by tools/summarize-notes.py in the GitHub Action "
            "(model = the Claude model that wrote it) or by "
            "hand (model \"hand\"). Edit \"bullets\" freely: an entry is kept until its channel's notes "
            "change. \"fold\": true adds the closing \"Plus smaller fixes and polish\" line. An entry with "
            "\"failed\" is ignored by the page and asked again after 7 days; delete it to try sooner. "
            "Keep this file valid JSON: while it does not parse, nothing is summarized.")

PRODUCT = {
    "bigmillz/concordeai": "ConcordeAI, a desktop AI assistant for Mac and Windows that runs models on the "
                           "user's own computer, with cloud models as an option",
    "bigmillz/concordevpn-releases": "ConcordeVPN, a Mac VPN app that runs through the user's own server",
    # no domain in here: a bullet that repeats one is rejected (tidy), and the
    # rejection is recorded for a week
    "concordego": "ConcordeGo, a flight-search website, in beta, that grades every flight "
                  "A+ to F by its all-in cost from your door, with points and miles, and Flight Fixer for a "
                  "delayed or cancelled flight; free, in any browser, nothing to download",
}
SPAN = {
    "stable": "These notes cover every build since the previous stable release.",
    "prerelease": "These notes cover every beta and release candidate since the last stable release.",
    "nightly": "These notes cover this nightly build: what is being worked on right now.",
    # ConcordeGo. Here, not in the prompt file: editing that re-keys every channel.
    "live": "ConcordeGo is a website, not a download: it deploys continuously, and there are no releases. "
            "These notes are the commit subjects of every change deployed in the 30 days up to the build "
            "the site runs now, newest first. Summarize them the way you would a prerelease span: what a "
            "visitor to the site would notice, as the site is now. Leave out anything about admin pages, "
            "allowlists, internal checks, tests, research notes, data tables and sources, servers and "
            "deployment, design mockups and prototypes (\"mock 10\", \"mockups\"), credits and data "
            "plumbing, unless it changes what a visitor sees. Commit subjects are terse and some pack "
            "several changes into one line; read them for the change, not the wording. Many subjects name "
            "where a number came from or how it was checked (research, seat maps, Skytrax, DOT records, a "
            "price check or parity with Google Flights): that means the figures on the page come from "
            "there, never that the site shows that source or promises to match it.",
    # What's new (tools/news-prompt.md): one release, or ConcordeGo's month
    "release": "These are the notes of one release (several builds that shared its title on one day are "
               "taken together). Name its one to three biggest changes for a one-line headline.",
    "go-news": "These are ConcordeGo's changes over the last 30 days, newest first, each starting with the date "
               "it went live. ConcordeGo is a website that deploys continuously. Pick only significant updates.",
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
PRICE_IN, PRICE_OUT = 4.0, 20.0          # Claude Opus 5.5, $ per million tokens
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


def ask(client, sync, system, repo, kind, label, items, cap=None):
    """One summary: (bullets, fold, model that answered). Raises Transient
    or Unusable; the chain runs most specific first."""
    import anthropic
    try:
        response = client.beta.messages.create(
            model=sync.MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                 # a refusal is re-run on Anthropic's recommended fallback
            thinking={"type": "adaptive"},      # always on for Opus 5.5; effort sets how much
            system=system,
            messages=[{"role": "user", "content": user_message(repo, kind, label, items)}],
            # medium is Opus 5.5's own default, set explicitly so a change of
            # default can never quietly change the notes
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": SCHEMA}},
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
    bullets, fold = tidy(data, sync.SHOW_MAX if cap is None else 99)
    if cap:
        bullets = bullets[:cap]
    return bullets, fold, getattr(response, "model", None) or sync.MODEL


def walk_go(sync, cache, stash, today, todo, referenced, complete):
    """ConcordeGo's one channel, the same way main() walks a product's: known,
    from the stash, or to do. Unreadable = skipped, its summaries kept.
    Only the build the live site names is summarized or kept: the branch
    tip that stands in when go.flyconcordefly.com cannot be read may be
    ahead of what is deployed, so it is never paid for, and nothing is
    pruned on its account (the deployed build's summary is still needed
    the moment the site answers again)."""
    try:
        go = sync.go_channel()
    except sync.Incomplete as exc:
        print("  go   %s — skipped, its summaries kept" % exc)
        return
    repo, kind, label, items = sync.GO_KEY, sync.GO_KIND, go["label"], go["items"]
    complete.add(repo)
    if not items:
        return
    ck = sync.channel_key(kind, items)
    referenced.setdefault(repo, set()).add(ck)
    if ck in (cache.get(repo) or {}) and not expired_failure(cache[repo][ck], today):
        print("  go   %-10s %s — cached (%s)" % (kind, label, cache[repo][ck].get("model")))
    elif ck in (stash.get(repo) or {}) and not expired_failure(stash[repo][ck], today):
        cache.setdefault(repo, {})[ck] = stash[repo][ck]
        print("  go   %-10s %s — from this run's stash" % (kind, label))
    else:
        todo.append(("go", repo, kind, label, items, ck, [go["build"]], {}))


def walk_news(sync, cache, stash, today, todo, referenced, complete):
    """What's new: one entry per release title a day in the last 30 days, and
    ConcordeGo's significant updates, cached under "news". Pruned only when
    every product was read whole."""
    try:
        system = sync.NEWS_PROMPT.read_text(encoding="utf-8")
    except OSError:
        print("  news no %s — not summarized" % sync.NEWS_PROMPT.name)
        return
    keep, whole = referenced.setdefault(sync.NEWS_KEY, set()), True
    wanted = []
    for key, repo, _buttons in sync.PRODUCTS:
        try:
            groups = sync.news_groups(repo)
        except (sync.Incomplete, SystemExit) as exc:
            print("  news %s: releases unreadable (%s) — its entries kept" % (key, exc))
            whole = False
            continue
        for date, title, rels in groups:
            items = sync.source_items(repo, rels)
            if items:
                wanted.append((key, repo, "release", "%s %s" % (key, title), items, sync.news_key("release", items)))
    try:
        go = sync.go_channel()
    except sync.Incomplete as exc:
        print("  news go: %s — its entry kept" % exc)
        whole = False
    else:
        items = sync.go_news_items(go)
        if items:
            wanted.append(("go", sync.GO_KEY, "go-news", "ConcordeGo, 30 days to %s" % go["label"],
                           items, sync.news_key("go", items)))
    for key, repo, kind, label, items, ck in wanted:
        keep.add(ck)
        have = cache.get(sync.NEWS_KEY) or {}
        if ck in have and not expired_failure(have[ck], today):
            print("  news %-10s %s — cached (%s)" % (kind, label, have[ck].get("model")))
        elif ck in (stash.get(sync.NEWS_KEY) or {}) and not expired_failure(stash[sync.NEWS_KEY][ck], today):
            cache.setdefault(sync.NEWS_KEY, {})[ck] = stash[sync.NEWS_KEY][ck]
            print("  news %-10s %s — from this run's stash" % (kind, label))
        else:
            todo.append((key, repo, kind, label, items, ck, sorted({i["tag"] for i in items}),
                         {"system": system, "cache": sync.NEWS_KEY, "cap": sync.NEWS_FEATURES}))
    if whole:
        complete.add(sync.NEWS_KEY)


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
                todo.append((key, repo, kind, label, items, ck, sorted({i["tag"] for i in items}), {}))

    walk_go(sync, cache, stash, today, todo, referenced, complete)     # after the apps
    walk_news(sync, cache, stash, today, todo, referenced, complete)   # last: the headlines

    if args.dry_run:
        for key, repo, kind, label, items, ck, _tags, _x in todo:
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

    for n, (key, repo, kind, label, items, ck, tags, x) in enumerate(todo):
        where = x.get("cache", repo)
        if clock() - started > TIME_BUDGET:
            print("  out of time: %d channel(s) left for the next run" % (len(todo) - n))
            break
        print("  %-4s %-10s %s — summarizing %d notes" % (key, kind, label, len(items)))
        entry = {"channel": "%s %s" % (kind, label), "source_tags": tags, "generated": today}
        try:
            bullets, fold, model = ask(client, sync, x.get("system", system), repo, kind, label, items, x.get("cap"))
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
        cache.setdefault(where, {})[ck] = entry
        write_json(sync.AUTO, cache)         # paid for: on disk now, even if the run is killed
        if args.stash:                       # …and kept through the workflow's reset
            stash.setdefault(where, {})[ck] = entry
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
