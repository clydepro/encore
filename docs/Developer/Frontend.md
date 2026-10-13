# Front end

What the guest interface is made of, and the rules that keep it working on a phone at the
far end of a house. Read this before adding a template, a swap target or a frame.

The shape is fixed by [ADR-002](../adr/ADR-002-htmx-instead-of-spa.md) (server-rendered HTML
plus HTMX, no SPA framework) and by [ADR-012](../adr/ADR-012-one-appliance-thread-the-event-loop-belongs-to-http.md)
(who renders, on which thread, and how often). The deviation from AIG 13's stack list — no
Tailwind, vendored htmx — is recorded in ADR-012's consequences and in
[`encore/static/vendor/NOTICE.md`](../../encore/static/vendor/NOTICE.md).

## The whole thing

```text
browser            htmx            server                    appliance thread
  │  GET /          │                │                              │
  │────────────────►│───────────────►│  read(appliance, work) ─────►│  SQLite, mpv
  │  shell HTML     │                │◄──────── views.player() ─────│
  │◄────────────────│◄───────────────│                              │
  │  EventSource /events ────────────►│  snapshot, facts, swap:<r>  │
  │  POST /queue (hx-post) ──────────►│  read(...) ─────────────────►│
  │        the new Up Next HTML ◄─────│  fragment, not a redirect   │
```

There is one JavaScript file, `encore/static/js/encore-live.js`, and it has one job: open
`/events` and put the server's HTML into the two named regions. There is no router, no client
state beyond "which song have I already drawn", and no fetch of anything except the appliance.

## Regions

A region is a top-level element with a stable `id`, that the server can render on its own, and
that the live layer can replace wholesale.

| Region | Element | Rendered by | Redrawn by these facts |
| ------ | ------- | ----------- | ---------------------- |
| `player` | `#player` (`panels/player.html`) | `/fragments/player`, `/` | `SongStarted`, `SongFinished`, `QueueAdvanced`, `LibraryReloaded` |
| `alerts` | `#alerts` (`panels/alerts.html`) | `/fragments/alerts`, `/` | `PlaybackRecovered`, `HealthChanged`, `LibraryReloaded` |

Two, not one-per-panel, because the fan-out is per region: each additional region is another
template render per fact for every screen in the room, and that cost is the party's, not the
architect's. `encore/api/fragments.py`'s `_REGIONS_BY_NAME` is the table; the SSE frame name is
`swap:` plus the region name.

A region renders from a `PlayerView`, which is one read of the appliance. Never render a region
by asking the services twice — two reads of a state that can change between them is a panel
that shows a song that already ended, and `tests/unit/test_fragments.py` asserts the count.

## htmx, used narrowly

The vocabulary in `encore/templates/` is deliberately small, and
`tests/unit/test_templates.py` fails on a token outside it:

- `hx-get`, `hx-post` — over `action`/`href`, never instead of them. A form must work when the
  script never loaded (SAPRS 13.2's "no single point of failure" reaches the front end).
- `hx-target` — `find <selector>` to move feedback into one line of a row, `#region` for a
  panel, and `closest .admin-region` on an administrative control, which is how a button inside
  a region says "redraw the thing you are in" without naming an id that a page has twice.
- `hx-swap` — `innerHTML` for the guest's regions. Administrative panels use `outerHTML`, on
  one condition that is load-bearing: the swapped element must be the region itself, with the
  same `id` and the same `hx-*` attributes, or the refresh dies with the swap that replaced it.
  Every `admin/panels/*.html` begins with the section that carries its own trigger for exactly
  that reason, and `tests/unit/test_templates.py` checks the shape. The one response that must
  not follow it is a failure: `partials/error.html` is a paragraph, not a region, so
  `errors.respond()` answers it with `HX-Reswap: innerHTML` and goes *into* the element instead of
  over it. Honouring `outerHTML` there would make a refused rebuild visible once and then leave a
  region with no `id` and no trigger, which is an interface that stops working in a way nobody can
  reproduce.
- `hx-swap-oob` — the `oob` flag on a panel template, so a fragment arriving over SSE can name
  its own target.
- `hx-trigger`, `hx-params` — the search box's debounce and its query parameter, and the
  administrative regions' `encore:change from:body delay:250ms` (see
  [Administrative pages](#administrative-pages)).
- `hx-confirm` — one use in the tree, on the queue-clear button. It is the only administrative
  action with no undo, and a native confirm is a modal that cannot be styled into lying about
  what it does; the alternative was a dialog component, which is more JavaScript than this
  project has. A browser without JavaScript gets no confirm and no `hx-*` at all — it gets the
  action and a dashboard that shows an empty Up Next, which is the honest version.

Rules:

- No `hx-boost`. Every swap is declared where it happens, so a link's cost is visible in the
  template that contains it.
- No `hx-on` handlers and no JavaScript in a template. If a screen needs behaviour that is not in
  the vocabulary, the behaviour belongs on the appliance.
- A fragment route answers with markup, not JSON. A machine client has `/api/v1`.
- **A failure that was asked for as a fragment is answered as one, and the bridge makes htmx
  render it.** This is the one place `encore-live.js` interferes with htmx's own decisions, and it
  exists because htmx's default `responseHandling` ends in `{code: "[45]..", swap: false}`: a 4xx
  or 5xx body is dropped without being read. That default assumes a server that answers a failure
  with a *page*; ours answers with the region's markup (`partials/error.html`), so a guest's
  "the queue holds 8 songs" and an operator's "that rebuild is not a database" were being computed
  correctly, sent correctly and then thrown away — an interface that appears to do nothing. The
  rule is narrow in the two ways that matter: it fires only on an error response, and only when
  the response is HTML, so a JSON envelope is never swapped into a region as text. The alternative
  was an `HX-Reswap` header at every failure site, which is eleven places that can forget it where
  this is one place that cannot.
- **A `401` that arrives at a panel is a door, not a message.** `errors.respond()` sends
  `HX-Redirect` with it, and htmx acts on that header before it consults its own table, so an
  expired session ends at the sign-in page with `?next=` back to the panel that asked. A swapped
  sentence containing a link is a clue; a navigation is the action. The fragment body still goes
  out, because a client that ignores the header should still read something true.
- Both halves of the two rules above are asserted in
  `tests/regression/test_issue_28_administration_interface.py` — including the vendored default
  this depends on, so an htmx upgrade that changes `responseHandling` fails a test rather than
  silently restoring an interface that appears to do nothing.

## The live layer

`encore-live.js` opens `/events` once per page and handles five frame names:

| Frame | What the client does |
| ----- | -------------------- |
| `snapshot` | Compare against what this screen has drawn; re-fetch the regions it can see if they differ |
| `SongQueued`, `SongStarted`, … | Ignore the body. It is the machine clients' contract; the redraw arrives as `swap:` |
| `swap:player`, `swap:alerts` | Replace that element's HTML. Never run a script from it, never parse it |
| `progress` | Move the bar and the clock, touching no structure |
| `resync` | Treat it as a `snapshot` |

Reconnect is a growing backoff (1 s → 30 s) and a fresh `snapshot`. There is no `Last-Event-ID`
handling in either direction, which removes a class of drift: the server never claims to have
kept history it did not keep.

`EventSource` cannot send a header, which was a note in this file about milestone 14 needing an
administrative stream. It does not have one, and the resolution is cheaper than the problem
looked: `EventSource` *does* send same-origin cookies, so an authenticated stream would have
worked — but an operator's laptop is one connection, and a second fan-out with its own queue per
tab, its own resync path and its own budget for a screen nobody in the room is watching is a
second live layer. The administrative pages refresh themselves from the facts the guest stream
was already sending. See [Administrative pages](#administrative-pages).

## Administrative pages

The same two regions, the same script, the same vocabulary. What is different is one event and
one convention.

The event is `encore:change`, dispatched by `encore-live.js` on `document.body` whenever a frame
says the appliance moved — a `swap:` of a region the screen can see, a `snapshot` that differs
from what it drew, or a `resync`. Every `admin/panels/*.html` region listens for it:

```text
hx-trigger="encore:change from:body delay:250ms"
```

That is the whole of the refresh design, and the two halves of it are decisions:

- **No polling.** Nothing under `/admin` asks for itself on a timer. A dashboard left open
  overnight on a kitchen tablet is a request every five seconds against a box designed to serve
  one disk (SAPRS 12.4), and `tests/integration/test_admin_interface.py` fails any admin page
  that contains `setInterval`, `setTimeout(` or a `refresh` meta — asserted as markup because a
  test cannot watch a network it did not build.
- **No new SSE region.** The guest frames are still `player` and `alerts`, and
  `REGIONS` in `encore-live.js` is still two names. An operator's panel refreshes by fetching its
  own HTML — one GET, on the laptop that is already open — which keeps the fan-out arithmetic in
  [Regions](#regions) true for the forty phones it was written about. The `delay:250ms` is
  htmx's throttle doing what a debounce would otherwise have to: a skip that publishes two facts
  repaints once.

The convention is that a screen exists twice. `admin/pages/dashboard.html` is the whole document,
for a browser that typed a URL; `admin/panels/dashboard.html` is the region, for `HX-Request`
and for the refresh. Both are rendered from one context builder (`encore/api/admin_views.py`),
so the two cannot describe two different appliances, and adding a screen means adding both files
and one function — a page that forgot its panel would answer an HTMX refresh with a whole
document inside a `<section>`, which is the markup equivalent of a wrong number.

An administrative control's response is the region that asked, named by `?panel=`. That is why
the dashboard's Pause button redraws the dashboard rather than the queue page: the alternative is
each of the ten screens knowing which of the others its buttons affect, which is a table nobody
maintains. A no-JavaScript browser gets a `303` to the page instead, and both branches perform
the command first — the shape of the bug in
`tests/regression/test_issue_28_administration_interface.py`.

An operator's screen also disagrees with a guest's in one visible way: it is allowed to be
*behind*. Guest markup comes from the appliance thread's own read at the moment of the fact;
an administrative panel is refetched a quarter of a second later, so the number it prints can be
newer than the one it replaced. That is a consequence of not adding a second stream, and it is
worth having written down rather than discovered during a party: the dashboard is a screen an
operator acts on, not the record of what the room heard.

## CSS

One file, `encore/static/css/encore.css`, hand-written, mobile-first, and no build step.
Conventions that are load-bearing:

- Block names match template names (`.song-row`, `.added--playing`, `.alerts--degraded`), so a
  grep finds the style from the markup.
- State classes are compound (`.added--playing`), never `!important`, because a state that wins
  by specificity order is a state that changes when someone reorders the file.
- Sizes are in `rem`, layout is grid/flex, and the tap target for the queue button is at least
  44 px at the narrowest breakpoint — a jukebox is used in the dark, with one hand, by someone
  holding a drink.
- No font and no icon is fetched. Inline SVG only, and the artwork placeholder is one of them.

## Testing a front-end change

- `tests/unit/test_templates.py` — the htmx vocabulary, and no absolute URL in a template
  (the offline rule, checked as an assertion rather than as a hope).
- `tests/unit/test_fragments.py` — regions, render counts, and what a fact costs in template
  renders.
- `tests/integration/test_server_app.py` — page versus fragment, the confirmation's wording,
  the frame ordering a room sees.
- `tests/e2e/test_guest_journey.py` — the walk a guest actually takes.
- `tests/integration/test_admin_interface.py` and `tests/e2e/test_admin_journey.py` — the same
  things from the other side of the console, including "no administrative page polls" and
  "nothing under `/admin` answers without a session".
- Coverage needs `encore/templates/` rendered by real Jinja, which the above do; there is no
  template-only test suite and adding one would be the wrong shape (a template that renders in
  isolation and is wired to nothing is the failure mode SAPRS 9.10 was written about).

Run them with `scripts/test.sh` or `uv run pytest tests/unit/test_fragments.py -q`.

## What is not here

- A client-side QR generator. `GET /qr` renders the poster on the appliance, server-side, from
  `ServerConfig.guest_url` ([API reference](../api/README.md), ADR-015): a code built in the browser
  would be encoding the address the *browser* guessed, which is the one thing a printed code cannot
  afford. The page is `templates/pages/qr.html`, and it is the only template that does not extend
  the guest shell — a poster wants paper margins, not a navigation bar.
- An administrative theme. The console reuses the guest stylesheet plus about ninety lines of
  `.admin-*`, and a second design system for a screen one person sees is not a thing this project
  needs.
- Sound. Nothing in this directory plays audio; playback is mpv, on the appliance thread, and
  the browser's only relationship to it is a number that moves.
