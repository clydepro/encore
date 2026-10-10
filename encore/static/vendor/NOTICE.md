# Vendored front-end assets

The appliance is expected to work with no uplink (SAPRS 13), so every asset the guest
interface needs is served from this directory rather than from a CDN. Nothing here is
fetched at runtime by anything else here.

## htmx 2.0.4

- File: `htmx/htmx.min.js`
- Version, as the bundle reports it itself: `2.0.4`
- sha256: `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` — computed
  from the bytes in this directory, and equal to upstream's own file. The hygiene hooks
  exclude this path so the hash stays recomputable by anyone who checks it out.
- Upstream: <https://github.com/bigskysoftware/htmx> — MIT licence
- Why vendored at all: ADR-002 chooses HTMX over a SPA framework for a LAN appliance, and a
  LAN appliance has no LAN cable for the CDN.
- Why this version: it is a released 2.x line, and it is the one the templates were written
  against. Only the vocabulary both 1.9 and 2.x agree on is used — `hx-get`, `hx-post`,
  `hx-target`, `hx-swap`, `hx-swap-oob`, `hx-trigger`, `hx-params` — so a bump within 2.x
  needs no template change and a downgrade to 1.9 would work. `tests/unit/test_templates.py`
  is what makes that claim checkable rather than hopeful: it reads every `hx-*` attribute out
  of `encore/templates/` and fails on a token this version does not recognise.
- The version boundary that could bite, and does not: htmx 2 changed how some form values are
  serialised. Encore's only form posts a single `song_id`, and
  `encore/controllers/queue.py` also accepts that value from a query string and from a JSON
  body, which is the shape a script and a browser agree on regardless of release.

## Not vendored, on purpose

- **Tailwind CSS.** The AIG's stack list names it; the interface ships a single
  hand-written `encore/static/css/encore.css` instead. Tailwind's browser build is a JIT
  that recompiles the stylesheet on every phone's first paint, and the alternative is a
  Node build step in a project whose deployment target is a `bash` script on a Pi. The
  deviation is recorded in ADR-012's consequences and in
  [`docs/Developer/Frontend.md`](../../../docs/Developer/Frontend.md).
- **The htmx SSE extension.** `encore/static/js/encore-live.js` replaces it: one
  `EventSource` per screen rather than one per `hx-ext` attribute, one reconnect policy, one
  place that decides what a stalled stream means (a `snapshot`, not a replay). The extension
  would also require `htmx.ajax` handling of `hx-swap-oob` naming, which the shell does not
  need because its regions have ids.
