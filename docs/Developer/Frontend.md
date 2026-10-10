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
- `hx-target` — `find <selector>` to move feedback into one line of a row, or `#region` for a
  panel. `hx-target="closest …"` is available and unused.
- `hx-swap` — `innerHTML`. Nothing else. A swap *style* is not a selector:
  `hx-swap="find .row-status"` is not htmx, whatever it looks like, and it was a bug here once.
- `hx-swap-oob` — the `oob` flag on a panel template, so a fragment arriving over SSE can name
  its own target.
- `hx-trigger`, `hx-params` — the search box's debounce and its query parameter.

Rules:

- No `hx-boost`. Every swap is declared where it happens, so a link's cost is visible in the
  template that contains it.
- No inline `hx-confirm`, no `hx-on` handlers, no JavaScript in a template. If a screen needs
  behaviour that is not in the vocabulary, the behaviour belongs on the appliance.
- A fragment route answers with markup, not JSON. A machine client has `/api/v1`.

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

`EventSource` cannot send a header, which is why the admin stream (milestone 14) will need a
same-origin cookie and a session that also works without a header. Nothing in this file solves
that yet, and the comment in `encore-live.js` saying so is the placeholder.

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
- Coverage needs `encore/templates/` rendered by real Jinja, which the above do; there is no
  template-only test suite and adding one would be the wrong shape (a template that renders in
  isolation and is wired to nothing is the failure mode SAPRS 9.10 was written about).

Run them with `scripts/test.sh` or `uv run pytest tests/unit/test_fragments.py -q`.

## What is not here

- Admin pages (milestone 14) — they will reuse both regions and must not add a third without a
  reason that survives the fan-out arithmetic above.
- QR posters (milestone 15) — `GET /qr` is documented as pending in the
  [API reference](../api/README.md).
- Sound. Nothing in this directory plays audio; playback is mpv, on the appliance thread, and
  the browser's only relationship to it is a number that moves.
