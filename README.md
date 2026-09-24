# flyconcordefly.com

The Concorde product site — ConcordeAI, ConcordeVPN and ConcordeGo.
One hand-written `index.html`, no build step. *Fly Concorde, Fly.*

The release data (versions, dates, download URLs) is baked into the
marked block inside `index.html`; update it when a release ships.
ConcordeVPN downloads come from the public
[concordevpn-releases](https://github.com/bigmillz/concordevpn-releases)
mirror — new VPN builds must be mirrored there.

## The contact form

The Feedback form in the About section posts to `/api/contact` in
`worker.js`, which checks a Cloudflare Turnstile token and then mails the
message through Email Routing. Until all four steps below are done the
Worker strips the form from the page and the endpoint answers 503:

1. Enable Email Routing on the flyconcordefly.com zone.
2. Verify the destination address in Email Routing.
3. Create a Turnstile widget: Managed mode, hostname `flyconcordefly.com`.
4. Add the Worker secrets `CONTACT_TO` (the verified address),
   `TURNSTILE_SITEKEY` and `TURNSTILE_SECRET` (both from the widget).

The `send_email` binding `CONTACT` is already in `wrangler.jsonc`, with no
address: this repo is public, so the address lives only in the secret.
Deleting any one of the three secrets takes the form down. Rejected checks
are logged as `contact: turnstile rejected` with Cloudflare's error codes,
and Cloudflare-side failures as `contact: siteverify failed` or
`contact: siteverify unreachable`; neither the token nor the visitor's
address is logged.
