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
 * 6. The contact form: POST /api/contact mails the message through
 *    Cloudflare Email Routing. The destination address is a Worker SECRET
 *    (CONTACT_TO), never in this repo — the repo is public. Without the
 *    binding and the secret the form is stripped from the page, so taking
 *    it down is deleting the secret.
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
      res = await stampUpdated(res, ctx);
      res = await stampNightlies(res, ctx);
      if (!contactReady(env)) res = stripContact(res);
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

/* ---------------------------------------------------------------- contact */

// The sender must be an address on a zone with Email Routing enabled; it
// needs no mailbox. The visitor's own address goes in Reply-To, never From,
// so the message passes the zone's own SPF/DMARC.
const CONTACT_FROM = "site@flyconcordefly.com";
const CONTACT_TOPICS = ["ConcordeAI", "ConcordeVPN", "ConcordeGo", "This website", "Something else"];
const CONTACT_PER_HOUR = 3;          // per visitor IP, per Cloudflare location

function contactReady(env) {
  return Boolean(env.CONTACT && env.CONTACT_TO);
}

function stripContact(res) {
  return new HTMLRewriter()
    .on("[data-contact]", { element(el) { el.remove(); } })
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
  if (Number(request.headers.get("Content-Length") || 0) > 20000) return reply(413, { ok: false, error: "That message is too long." });

  let data;
  try { data = await request.json(); } catch (_) { return reply(400, { ok: false, error: "That didn’t send. Try again." }); }
  const field = (v, max) => (typeof v === "string" ? v : "").trim().slice(0, max);

  // Bots: the hidden field is filled, or the form was never run as a page,
  // or it was submitted faster than a person can type. Answer as if it
  // worked, so there is nothing to tune against.
  if (field(data.website, 200) || data.js !== 1 || !(Number(data.ms) >= 2500)) {
    return reply(200, { ok: true });
  }

  // Header values must never carry a line break (header injection).
  const oneLine = (v) => v.replace(/[\r\n\u2028\u2029]+/g, " ").replace(/[<>"]/g, "");
  const name = oneLine(field(data.name, 100));
  const email = oneLine(field(data.email, 200));
  const topic = CONTACT_TOPICS.includes(data.product) ? data.product : "Something else";
  const message = field(data.message, 5000);
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return reply(400, { ok: false, error: "Add an email address so I can reply." });
  if (message.length < 5) return reply(400, { ok: false, error: "Write a message first." });

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
