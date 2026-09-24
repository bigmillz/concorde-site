/**
 * concorde-site — static assets plus the things GitHub Pages could not do.
 *
 * 1. HSTS. Pages sends no response headers you control, so a first-time
 *    visitor typing the bare domain always made one plaintext request.
 * 2. http -> https, as a real 301 rather than serving both.
 * 3. www -> apex, so the site has one canonical origin.
 * 4. The footer's "Updated" date, taken from the repo's latest commit so it
 *    moves on every push instead of whenever someone remembers to edit it.
 * 5. The nightly channels' commit and build date, read from GitHub at
 *    request time: a nightly is rebuilt on every push and the page's
 *    generated copy of it is only as fresh as the last sync.
 * 6. The contact form: POST /api/contact checks a Cloudflare Turnstile
 *    token, then mails the message through Cloudflare Email Routing. The
 *    destination address is a Worker SECRET (CONTACT_TO), never in this
 *    repo — the repo is public. The form needs the send_email binding and
 *    three secrets (CONTACT_TO, TURNSTILE_SITEKEY, TURNSTILE_SECRET);
 *    without all four it is stripped from the page, so taking it down is
 *    deleting any one of the secrets.
 * 7. ConcordeGo's build and date, read from go.flyconcordefly.com itself.
 *    It is a website, not a release: it deploys on every push and has no
 *    GitHub release for tools/sync-releases.py to read, so its card is
 *    hand-written and only these two values are kept live.
 *
 * These live here rather than in dashboard toggles so they travel with the
 * repo and are reviewable. `run_worker_first` in wrangler.jsonc is what
 * guarantees this runs for asset requests too — without it, static files
 * are served before the Worker ever sees them, and none of the headers
 * below would apply to the pages people actually load.
 */

import { EmailMessage } from "cloudflare:email";

const CANONICAL = "flyconcordefly.com";
// One year. No `preload`: that is a one-way door that needs a separate
// submission and is painful to unwind, so it stays a deliberate choice.
const HSTS = "max-age=31536000; includeSubDomains";

// The commit feed is plain Atom with no auth and none of the API's
// per-IP rate limit, which matters because every cache miss hits it.
const COMMITS_FEED = "https://github.com/bigmillz/concorde-site/commits/main.atom";
// How stale the footer may be after a push. Workers Builds takes ~20s to
// deploy, so anything much tighter than this buys nothing.
const UPDATED_TTL = 600;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// The rolling nightly per product. Read from the release's own page rather
// than the API (60 requests/hour per IP unauthenticated, and Cloudflare's
// egress IPs are shared) or the Atom feed (which orders by the tagged
// commit's date, so the mirror's nightly never surfaces in it).
const NIGHTLIES = { ai: "bigmillz/concordeai", vpn: "bigmillz/concordevpn-releases" };
const NIGHTLY_TTL = 300;

// ConcordeGo publishes what is deployed in its own footer, machine-readably:
//   <time id="updated" data-updated="2026-09-22">…</time> · build <code id="build" data-build="6ccedc4">…</code>
// The page is ~2.7 MB and the stamp sits in its first ~100 KB, so it is
// read only until the stamp has gone by (GO_SCAN_MAX bounds a page that
// has lost it). A miss is cached for GO_TTL too: a page without its stamp
// costs one read per location every five minutes, not one per page view.
const GO_URL = "https://go.flyconcordefly.com/";
const GO_TTL = 300;
const GO_SCAN_MAX = 512 * 1024;
const GO_BUILD = /\bdata-build="([0-9a-f]{7,40})"/i;
const GO_UPDATED = /\bdata-updated="(\d{4}-\d{2}-\d{2})"/;
// each chunk is searched with this many characters of the text before it,
// so a stamp split across two chunks is still found (both are < 60 long)
const GO_OVERLAP = 128;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    // The *.workers.dev copy is staging and `wrangler dev` is local; neither
    // gets the canonical redirect or HSTS (which on workers.dev would pin
    // the shared parent domain).
    const isWorkersDev = url.hostname.endsWith(".workers.dev")
      || url.hostname === "localhost" || url.hostname === "127.0.0.1";

    if (!isWorkersDev) {
      const wrongHost = url.hostname !== CANONICAL;
      const wrongProto = url.protocol !== "https:";
      if (wrongHost || wrongProto) {
        url.hostname = CANONICAL;
        url.protocol = "https:";
        url.port = "";
        return new Response(null, {
          status: 301,
          headers: {
            Location: url.toString(),
            "Strict-Transport-Security": HSTS,
          },
        });
      }
    }

    if (url.pathname === "/api/contact") {
      return withHeaders(await handleContact(request, env, ctx, isWorkersDev), isWorkersDev);
    }

    let res = await env.ASSETS.fetch(request);
    if ((res.headers.get("content-type") || "").startsWith("text/html")) {
      const go = goInfo(ctx).catch(() => null);   // in flight while the two below run
      res = await stampUpdated(res, ctx);
      res = await stampNightlies(res, ctx);
      res = stampGo(res, await go);
      res = contactReady(env) ? stampTurnstile(res, env) : stripContact(res);
    }
    if (isWorkersDev) return res;

    res = new Response(res.body, res);
    res.headers.set("Strict-Transport-Security", HSTS);
    // Cheap, uncontroversial hardening. No CSP here: the page pulls fonts
    // from Google and would need a real policy written against it, which is
    // a separate change with its own testing.
    res.headers.set("X-Content-Type-Options", "nosniff");
    res.headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
    return res;
  },
};

/** A stalled upstream must never hold the page: race every outbound read
 *  against a timer. AbortSignal.timeout alone is not enough — a connection
 *  stuck before the response (seen with a DNS-intercepting VPN in local dev)
 *  ignored it and the page waited minutes. Resolves null on timeout. */
function within(ms, promise) {
  let timer;
  return Promise.race([
    promise,
    new Promise((resolve) => { timer = setTimeout(() => resolve(null), ms); }),
  ]).finally(() => clearTimeout(timer));
}

/** Rewrite <time id="updated"> to the latest commit date. On any failure
 *  the page goes out untouched with the date baked into index.html. */
async function stampUpdated(res, ctx) {
  const iso = await latestCommitDate(ctx);
  if (!iso) return res;
  const d = new Date(iso);
  const label = `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  return new HTMLRewriter()
    .on("time#updated", {
      element(el) {
        el.setAttribute("datetime", iso.slice(0, 10));
        el.setInnerContent(label);
      },
    })
    .transform(res);
}

async function latestCommitDate(ctx) {
  const cache = caches.default;
  // A synthetic URL on our own origin: the cache is keyed by request, and
  // this must never collide with a real asset.
  const key = new Request(`https://${CANONICAL}/.cache/updated`);
  try {
    const hit = await cache.match(key);
    if (hit) return await hit.text();
  } catch (_) { /* no cache in this runtime — fetch every time */ }

  let iso = null;
  try {
    const r = await within(2500, fetch(COMMITS_FEED, {
      headers: { "User-Agent": "concorde-site-worker (+https://flyconcordefly.com)" },
      signal: AbortSignal.timeout(2500),
    }));
    const body = r && r.ok ? await within(1500, r.text()) : null;
    if (body) {
      // The feed-level <updated> is the newest entry's commit time and
      // comes before any <entry>, so the first match is the one we want.
      const m = body.match(/<updated>(\d{4}-\d{2}-\d{2}T[^<]+)<\/updated>/);
      if (m && !Number.isNaN(Date.parse(m[1]))) iso = m[1];
    }
  } catch (_) {
    return null;
  }
  if (!iso) return null;

  try {
    ctx.waitUntil(cache.put(key, new Response(iso, {
      headers: { "Cache-Control": `max-age=${UPDATED_TTL}` },
    })));
  } catch (_) { /* same: fine without */ }
  return iso;
}

/** Rewrite each nightly block's commit and date from GitHub. The page's
 *  generated values stay as the fallback whenever a fetch fails. */
async function stampNightlies(res, ctx) {
  const live = {};
  await Promise.all(Object.entries(NIGHTLIES).map(async ([key, repo]) => {
    const info = await nightlyInfo(repo, ctx);
    if (info) live[key] = info;
  }));
  if (!Object.keys(live).length) return res;
  // an ops breadcrumb: `curl -I` shows what the edge read from GitHub
  res = new Response(res.body, res);
  res.headers.set("X-Nightly", Object.entries(live).map(([k, v]) => `${k}=${v.sha}`).join(" "));
  const rw = new HTMLRewriter();
  for (const [key, { ver, sha, date }] of Object.entries(live)) {
    const d = new Date(date);
    const label = `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
    // Only when the live nightly is still the version this block was built
    // for. The download buttons are generated and cannot be rewritten here,
    // so on a version bump the whole block stays as generated — stale, but
    // honest and with a link that works — until the next sync.
    let sameVersion = true;
    rw.on(`[data-nightly-commit="${key}"]`, {
      element(el) {
        sameVersion = el.getAttribute("data-nightly-ver") === ver;
        if (!sameVersion) return;
        const base = el.getAttribute("data-commit-base");
        if (base) el.setInnerContent(`<a href="${base}${sha}">${sha}</a>`, { html: true });
        else el.setInnerContent(sha);
      },
    });
    rw.on(`[data-nightly-date="${key}"]`, {
      element(el) {
        if (!sameVersion) return;       // the commit span precedes this one
        el.setAttribute("datetime", date.slice(0, 10));
        el.setInnerContent(label);
      },
    });
  }
  return rw.transform(res);
}

async function nightlyInfo(repo, ctx) {
  const cache = caches.default;
  const key = new Request(`https://${CANONICAL}/.cache/nightly/${repo}`);
  try {
    const hit = await cache.match(key);
    if (hit) return await hit.json();
  } catch (_) { /* no cache in this runtime */ }

  let info = null;
  try {
    const r = await within(2500, fetch(`https://github.com/${repo}/releases/tag/nightly`, {
      headers: { "User-Agent": "concorde-site-worker (+https://flyconcordefly.com)", Accept: "text/html" },
      signal: AbortSignal.timeout(2500),
    }));
    const html = r && r.ok ? await within(1500, r.text()) : null;
    if (html) {
      // "<title>Release 1.3 nightly 7caf38d · bigmillz/concordevpn-releases</title>"
      const t = html.match(/<title>Release\s+(.+?)\s+nightly\s+([0-9a-f]{7,40})\b/i);
      // the first timestamp on the page is the release's own
      const d = html.match(/datetime="(\d{4}-\d{2}-\d{2}T[^"]+)"/);
      if (t && d) info = { ver: t[1].trim(), sha: t[2].slice(0, 7), date: d[1] };
    }
  } catch (_) {
    return null;
  }
  if (!info) return null;

  try {
    ctx.waitUntil(cache.put(key, new Response(JSON.stringify(info), {
      headers: { "Content-Type": "application/json", "Cache-Control": `max-age=${NIGHTLY_TTL}` },
    })));
  } catch (_) { /* fine without */ }
  return info;
}

/** Rewrite ConcordeGo's build and "Updated" date ([data-go-build],
 *  time[data-go-date]) from what the live app says it is running. On any
 *  failure the page keeps the values baked into index.html. */
function stampGo(res, info) {
  if (!info) return res;
  const d = new Date(`${info.date}T00:00:00Z`);
  const label = `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  res = new Response(res.body, res);
  res.headers.set("X-Go", `${info.sha} ${info.date}`);   // `curl -I` breadcrumb, like X-Nightly
  return new HTMLRewriter()
    .on("[data-go-build]", { element(el) { el.setInnerContent(info.sha); } })
    .on("time[data-go-date]", {
      element(el) {
        el.setAttribute("datetime", info.date);
        el.setInnerContent(label);
      },
    })
    .transform(res);
}

async function goInfo(ctx) {
  const cache = caches.default;
  const key = new Request(`https://${CANONICAL}/.cache/go`);
  try {
    const hit = await cache.match(key);
    if (hit) {
      const j = await hit.json();
      return j && j.sha ? j : null;       // {miss: true}: read and failed lately
    }
  } catch (_) { /* no cache in this runtime */ }

  let info = null;
  try {
    const r = await within(2500, fetch(GO_URL, {
      headers: { "User-Agent": "concorde-site-worker (+https://flyconcordefly.com)", Accept: "text/html" },
      signal: AbortSignal.timeout(2500),
    }));
    if (r && r.ok && r.body) info = await within(1500, scanGo(r.body));
  } catch (_) { /* info stays null, and the miss is cached below */ }

  try {
    ctx.waitUntil(cache.put(key, new Response(JSON.stringify(info || { miss: true }), {
      headers: { "Content-Type": "application/json", "Cache-Control": `max-age=${GO_TTL}` },
    })));
  } catch (_) { /* fine without */ }
  return info;
}

/** Read a body only until both halves of the stamp have gone by, then hang
 *  up. Each chunk is searched once (with GO_OVERLAP characters of the text
 *  before it), never the whole text so far again, so a page that has lost
 *  its stamp costs one pass over GO_SCAN_MAX, not one per chunk. */
async function scanGo(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let carry = "", bytes = 0, build = null, updated = null;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      const text = carry + (done ? decoder.decode() : decoder.decode(value, { stream: true }));
      if (!build) build = (text.match(GO_BUILD) || [])[1] || null;
      if (!updated) updated = (text.match(GO_UPDATED) || [])[1] || null;
      if (build && updated) return goStamp(build, updated);
      if (done) return null;
      bytes += value.byteLength;
      if (bytes > GO_SCAN_MAX) return null;
      carry = text.slice(-GO_OVERLAP);
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

/** { sha, date } from ConcordeGo's footer stamp, or null. Both must be
 *  there and well formed: a half-read or reshaped page changes nothing.
 *  The date must be a real one: Date.parse takes 2026-02-31 as 3 March,
 *  which would put one date in datetime and another in the text. */
function goStamp(build, updated) {
  const d = new Date(`${updated}T00:00:00Z`);
  if (Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== updated) return null;
  return { sha: build.slice(0, 7).toLowerCase(), date: updated };
}

/* ---------------------------------------------------------------- contact */

// The sender must be an address on a zone with Email Routing enabled; it
// needs no mailbox. The visitor's own address goes in Reply-To, never From,
// so the message passes the zone's own SPF/DMARC.
const CONTACT_FROM = "site@flyconcordefly.com";
const CONTACT_TOPICS = ["ConcordeAI", "ConcordeVPN", "ConcordeGo", "This website", "Something else"];
const CONTACT_PER_HOUR = 3;          // per visitor IP, per Cloudflare location
const CONTACT_MAX_BYTES = 20000;     // the JSON body; the message itself is cut at 5000 characters

// Turnstile, Cloudflare's CAPTCHA: no third-party tracker, and most people
// never see it. The widget is created in the dashboard (Managed mode,
// hostname flyconcordefly.com); its site key and secret are Worker secrets.
// The site key is public, but as a secret rather than a wrangler.jsonc var
// a Workers Builds deploy can never wipe it, and creating the widget needs
// no repo edit.
const SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify";
const TURNSTILE_ACTION = "contact";  // the page renders the widget with this action
const TOKEN_MAX = 2048;              // Cloudflare's documented maximum token length

// All four or nothing; README.md, "The contact form", has the switch-on steps.
function contactReady(env) {
  return Boolean(env.CONTACT && env.CONTACT_TO && env.TURNSTILE_SITEKEY && env.TURNSTILE_SECRET);
}

function stripContact(res) {
  return new HTMLRewriter()
    .on("[data-contact]", { element(el) { el.remove(); } })
    .transform(res);
}

/** Put the Turnstile site key on the form's widget box. The key is never in
 *  index.html, so a page served without it has no key to render with. */
function stampTurnstile(res, env) {
  return new HTMLRewriter()
    .on("[data-turnstile]", { element(el) { el.setAttribute("data-sitekey", env.TURNSTILE_SITEKEY); } })
    .transform(res);
}

function withHeaders(res, isWorkersDev) {
  if (isWorkersDev) return res;
  res = new Response(res.body, res);
  res.headers.set("Strict-Transport-Security", HSTS);
  res.headers.set("X-Content-Type-Options", "nosniff");
  res.headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  return res;
}

function reply(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

async function handleContact(request, env, ctx, isWorkersDev) {
  if (request.method !== "POST") return reply(405, { ok: false, error: "Use the form on the page." });
  if (!contactReady(env)) return reply(503, { ok: false, error: "The contact form is not available right now." });

  // Only the page itself may post here.
  const origin = request.headers.get("Origin") || "";
  let originHost = "";
  try { originHost = new URL(origin).hostname; } catch (_) { /* no or bad Origin */ }
  if (originHost !== CANONICAL && !isWorkersDev) return reply(403, { ok: false, error: "Use the form on the page." });
  if (Number(request.headers.get("Content-Length") || 0) > CONTACT_MAX_BYTES) return reply(413, { ok: false, error: "That message is too long." });

  // The header is only a fast path: a chunked or HTTP/2 post can leave it
  // out, so the body itself is counted as it is read.
  let data;
  try {
    const text = await readCapped(request, CONTACT_MAX_BYTES);
    if (text === null) return reply(413, { ok: false, error: "That message is too long." });
    data = JSON.parse(text);
  } catch (_) { data = null; }
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return reply(400, { ok: false, error: "That didn’t send. Try again." });
  }
  const field = (v, max) => (typeof v === "string" ? v : "").trim().slice(0, max);

  // Bots: the hidden field is filled, or the form was never run as a page,
  // or it was submitted faster than a person can type. Answer as if it
  // worked, so there is nothing to tune against.
  if (field(data.website, 200) || data.js !== 1 || !(Number(data.ms) >= 2500)) {
    return reply(200, { ok: true });
  }

  // Header values must never carry a line break (header injection).
  const oneLine = (v) => v.replace(/[\r\n\u2028\u2029]+/g, " ").replace(/[<>"\\]/g, "");
  const name = oneLine(field(data.name, 100));
  const email = oneLine(field(data.email, 200));
  const topic = CONTACT_TOPICS.includes(data.product) ? data.product : "Something else";
  const message = field(data.message, 5000);
  if (!/^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$/.test(email)) return reply(400, { ok: false, error: "Add an email address I can reply to." });
  if (message.length < 5) return reply(400, { ok: false, error: "Write a message first." });

  // Turnstile before the hourly limit: a post without a valid token must not
  // use up the allowance of a real visitor behind the same IP.
  const human = await turnstileOk(data.token, request, env, isWorkersDev);
  if (human === null) return reply(502, { ok: false, error: "The spam check couldn’t be reached. Try again in a minute." });
  if (!human) return reply(403, { ok: false, error: "The spam check didn’t pass, so nothing was sent. Try again, or reload the page." });

  if (!(await underLimit(request, ctx))) {
    return reply(429, { ok: false, error: "Too many messages from here. Try again in an hour." });
  }

  const cf = request.cf || {};
  const body = [
    `From:  ${name || "(no name)"} <${email}>`,
    `About: ${topic}`,
    `Where: ${[cf.city, cf.country].filter(Boolean).join(", ") || "unknown"}`,
    "",
    message,
    "",
    "--",
    "Sent from the contact form on https://flyconcordefly.com/#about",
    "Reply to this email to answer them.",
  ].join("\n");

  const raw = mime({
    from: `flyconcordefly.com <${CONTACT_FROM}>`,
    to: env.CONTACT_TO,
    replyTo: name ? `${encodeWord(name)} <${email}>` : email,
    subject: `[flyconcordefly] ${topic}${name ? " — " + name : ""}`,
    text: body,
  });
  try {
    await env.CONTACT.send(new EmailMessage(CONTACT_FROM, env.CONTACT_TO, raw));
  } catch (err) {
    console.log("contact: send failed", String(err));
    return reply(502, { ok: false, error: "That didn’t send. Try again in a minute." });
  }
  return reply(200, { ok: true });
}

/** true if Cloudflare vouches for the token, false if it does not, null if
 *  siteverify could not be asked. Every path that is not a clear yes sends
 *  nothing. Tokens are single-use and expire after five minutes; the page
 *  resets its widget after every attempt. */
async function turnstileOk(token, request, env, isWorkersDev) {
  if (typeof token !== "string" || !token || token.length > TOKEN_MAX) return false;
  let r;
  try {
    const res = await within(4000, fetch(SITEVERIFY, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        secret: env.TURNSTILE_SECRET,
        response: token,
        remoteip: request.headers.get("CF-Connecting-IP") || undefined,
        // one per check; siteverify answers a repeat of the same key as a
        // retry of that check rather than as a second use of the token
        idempotency_key: crypto.randomUUID(),
      }),
      signal: AbortSignal.timeout(4000),
    }));
    // A bad secret or request comes back 400 with the usual JSON (error
    // code invalid-input-secret, say), so the body is read whatever the
    // status: that is what makes the log below say what went wrong.
    r = res ? await within(1500, res.json().catch(() => null)) : null;
    // A 5xx, or internal-error in any answer, is Cloudflare's side failing
    // (documented as retryable), not a verdict on the visitor.
    if (r && typeof r === "object" && (res.status >= 500 || [].concat(r["error-codes"] || []).includes("internal-error"))) {
      console.log("contact: siteverify failed", JSON.stringify({ status: res.status, codes: r["error-codes"] || [] }));
      return null;
    }
  } catch (_) {
    r = null;
  }
  if (!r || typeof r !== "object" || typeof r.success !== "boolean") {
    console.log("contact: siteverify unreachable");
    return null;
  }
  // Cloudflare's test keys answer for hostname example.com and send no
  // action, so staging and local dev skip the hostname and accept a missing
  // action. A wrong action fails everywhere.
  const actionOk = r.action === TURNSTILE_ACTION || (isWorkersDev && r.action === undefined);
  const hostOk = isWorkersDev || r.hostname === CANONICAL;
  if (r.success === true && actionOk && hostOk) return true;
  console.log("contact: turnstile rejected", JSON.stringify({
    codes: r["error-codes"] || [], action: r.action, hostname: r.hostname,
  }));
  return false;
}

/** The body as text, or null once it passes max bytes (the rest is not read). */
async function readCapped(request, max) {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let n = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    n += value.byteLength;
    if (n > max) { reader.cancel().catch(() => {}); return null; }
    chunks.push(value);
  }
  const all = new Uint8Array(n);
  let at = 0;
  for (const c of chunks) { all.set(c, at); at += c.byteLength; }
  return new TextDecoder().decode(all);
}

async function underLimit(request, ctx) {
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const hour = Math.floor(Date.now() / 3600000);
  const key = new Request(`https://${CANONICAL}/.cache/contact/${encodeURIComponent(ip)}/${hour}`);
  try {
    const hit = await caches.default.match(key);
    const n = hit ? Number(await hit.text()) || 0 : 0;
    if (n >= CONTACT_PER_HOUR) return false;
    ctx.waitUntil(caches.default.put(key, new Response(String(n + 1), {
      headers: { "Cache-Control": "max-age=3600" },
    })));
  } catch (_) { /* no cache here: let it through */ }
  return true;
}

// RFC 2047 encoded-word, for any header value that is not plain ASCII.
function encodeWord(s) {
  return /^[\x20-\x7e]*$/.test(s) ? `"${s}"` : `=?UTF-8?B?${b64(s)}?=`;
}

function b64(s) {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

// A minimal, correct text/plain message: base64 body so any language and
// any line length survive transport untouched.
function mime({ from, to, replyTo, subject, text }) {
  const body = b64(text).replace(/.{1,76}/g, "$&\r\n");
  return [
    `From: ${from}`,
    `To: <${to}>`,
    `Reply-To: ${replyTo}`,
    `Subject: ${/^[\x20-\x7e]*$/.test(subject) ? subject : `=?UTF-8?B?${b64(subject)}?=`}`,
    `Date: ${new Date().toUTCString()}`,
    `Message-ID: <${crypto.randomUUID()}@${CANONICAL}>`,
    "MIME-Version: 1.0",
    "Content-Type: text/plain; charset=utf-8",
    "Content-Transfer-Encoding: base64",
    "",
    body,
  ].join("\r\n");
}
