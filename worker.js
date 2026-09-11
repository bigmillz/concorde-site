/**
 * concorde-site — static assets plus the three things GitHub Pages could not do.
 *
 * 1. HSTS. Pages sends no response headers you control, so a first-time
 *    visitor typing the bare domain always made one plaintext request.
 * 2. http -> https, as a real 301 rather than serving both.
 * 3. www -> apex, so the site has one canonical origin.
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

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const isWorkersDev = url.hostname.endsWith(".workers.dev");

    // Leave the *.workers.dev preview alone: it is the staging copy, and
    // sending HSTS for it would pin the shared workers.dev parent domain.
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

    const asset = await env.ASSETS.fetch(request);
    if (isWorkersDev) return asset;

    const res = new Response(asset.body, asset);
    res.headers.set("Strict-Transport-Security", HSTS);
    // Cheap, uncontroversial hardening. No CSP here: the page pulls fonts
    // from Google and would need a real policy written against it, which is
    // a separate change with its own testing.
    res.headers.set("X-Content-Type-Options", "nosniff");
    res.headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
    return res;
  },
};
