/**
 * concorde-site — static assets plus the things GitHub Pages could not do.
 *
 * 1. HSTS. Pages sends no response headers you control, so a first-time
 *    visitor typing the bare domain always made one plaintext request.
 * 2. http -> https, as a real 301 rather than serving both.
 * 3. www -> apex, so the site has one canonical origin.
 * 4. The footer's "Updated" date, taken from the repo's latest commit so it
 *    moves on every push instead of whenever someone remembers to edit it.
 *
 * These live here rather than in dashboard toggles so they travel with the
 * repo and are reviewable. `run_worker_first` in wrangler.jsonc is what
 * guarantees this runs for asset requests too — without it, static files
 * are served before the Worker ever sees them, and none of the headers
 * below would apply to the pages people actually load.
 */

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

    let res = await env.ASSETS.fetch(request);
    if ((res.headers.get("content-type") || "").startsWith("text/html")) {
      res = await stampUpdated(res, ctx);
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
    const r = await fetch(COMMITS_FEED, {
      headers: { "User-Agent": "concorde-site-worker (+https://flyconcordefly.com)" },
      signal: AbortSignal.timeout(2500),
    });
    if (r.ok) {
      // The feed-level <updated> is the newest entry's commit time and
      // comes before any <entry>, so the first match is the one we want.
      const m = (await r.text()).match(/<updated>(\d{4}-\d{2}-\d{2}T[^<]+)<\/updated>/);
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
