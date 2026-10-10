# Current Phase

**Phase 4 — runtime composition, HTTP, HTMX, SSE (AIG steps 11, 12, 13): implemented on
`feat/25-runtime-server-htmx-sse`, open as a pull request against `main` for issue
[#25](https://github.com/clydepro/encore/issues/25).** Phases 1–3 are merged:
[PR #20](https://github.com/clydepro/encore/pull/20) (domain, bus, configuration),
[PR #22](https://github.com/clydepro/encore/pull/22) (repositories, Builder — closed #19) and
[PR #24](https://github.com/clydepro/encore/pull/24) (search, playback, queue — closed #23).
This branch sits on `main` at `4d30b42` and needs nothing from an unmerged sibling.

There is now something to start. `scripts/run-server.sh` brings up an appliance that serves
pages, fragments, a JSON API and a live stream over the real graph, and 40 phones can watch it
at once. What is *not* here: the administrative interface, login, statistics screens, log tail
and QR (milestone 14), the installer (15), and the Party Simulation driver (16).

Read this page first in a new session, then [`context/milestones.md`](context/milestones.md)
for what exists and what is scaffolding. Neither is authoritative: precedence is
task request → SAPRS → AIG → ADRs → AEP (AEP 2).

## Delivered

| AIG 21 | Delivered as | Spec |
| ------ | ------------ | ---- |
| 11 — FastAPI | `encore/api/`, `apps/server/` | SAPRS Ch. 10, ADR-012 |
| 12 — HTMX | `encore/controllers/`, `encore/templates/`, `encore/static/` | SAPRS Ch. 9, ADR-002 |
| 13 — SSE | `encore/services/sse_publisher.py`, `encore/api/sse.py` | SAPRS 9.10, 10.5 |

Steps 10 (runtime database) and the runtime half of the AIG's list were already done in phase 2
and 3 respectively; this phase's job was to make them *reachable*.

**Composition** is `apps/server/appliance.py::build()`: it opens both stores, wires the bus,
the queue, the player, the supervisor, health, search and the publisher into one object, and
starts them in the order the facts require — subscriptions before the queue's re-adoption, the
queue before mpv, the player before the tick loop. `stop()` unwinds it. Every test that needs a
server calls this through `tests/support/appliances.py::composed_appliance()` with a fake
launcher, so the shipped wiring is the wiring under test rather than a parallel copy of it.

**The thread boundary** is one function: `encore/api/deps.py::read()` awaits
`encore/utilities/appliance.py::call_async()`, which hands a closure to the appliance thread and
raises `ApplianceNotRunning` or `WrongThreadError` if the arrangement is broken. ADR-012 argued
for it; this phase is where it stopped being theoretical. `ApplianceThread.worker_ident` exists
because a guardrail that cannot name the thread that answered a request is a comment — the
integration assertion asks a served response which thread did the work.

**The HTTP layer** splits the way the AIG asks: `api/` (app, deps, errors, html, schemas,
views, rows, sse, fragments) holds mechanisms, `controllers/` (browse, panels, queue) holds
routes. `errors.py` is a single table from exception to status to code, so a failure says the
same kind of word wherever it happens; `rows.py` is the batched label read that keeps a page at
two statements rather than `3 × rows`, which is what the 100 ms search budget leans on;
`views.py` builds contexts and nothing else. Templates contain presentation only, and the one
piece of client JavaScript is the SSE bridge.

**The UI** is a shell with two named swap regions — `swap:player` and `swap:alerts` — htmx
vendored at `static/vendor/htmx/htmx.min.js`, hand-written CSS, and `POST /queue` answering with
the new Up Next rather than a redirect. A guest's second press says "Added at 2" when a song is
already playing, and the wording comes from the queue item's status rather than from what the
player happened to be doing, which is SAPRS 9.7 and was a bug before it was a sentence.

**SSE** is `SSEPublisher`: one bounded queue (32 frames) per connection, a `snapshot` frame
first so a late join needs no replay, facts named by their class so the wire vocabulary cannot
drift from `EVENT_VOCABULARY`, `progress` once a second, `resync` when a stream stalls, and
region HTML rendered *once per fact* for every screen rather than once per screen. A fact and a
region are separate frames on purpose: a JSON client should not parse HTML, and a screen should
not wait on a database it cannot see.

## Numbers

1109 tests pass in the standard gate (`scripts/check.sh`), 1146 with the slow suites
untraced;
9 skip for want of `--run-slow`, 1 for the missing Party Simulation driver. Coverage of
`encore/` is **92.7%**; `fail_under` remains 0 as it was set at bootstrap and the raising of it
is a decision for milestone 15's PR rather than a side effect of this one.

Reference p95s from the development machine (ARM64, untraced), all inside SAPRS 1.8's budgets:

| Budget | Target | Measured |
| ------ | ------ | -------- |
| Search page | 100 ms | 13.6 ms |
| Queue mutation | 50 ms | 34.6 ms |
| Navigation fragment | 200 ms | 9.5 ms |
| SSE propagation, one fact → 40 screens | 1 s | 8.7 ms wall, 1.3 ms per screen |

The last row improved 10× during this phase, and not because of a faster machine: the fan-out
moved off the appliance thread onto the event loop, which is ADR-012's argument working as
designed. `tests/performance/test_performance_targets.py`'s `UNIMPLEMENTED` list is now empty —
every named budget has a measurement or an explicit skip reason.

## Ten defects, and what they were only visible in

All eleven are in `tests/regression/test_issue_25_http_runtime.py` with the table in its
docstring. The pattern is worth naming for the next phase: nine of them passed every unit test that
existed, and all ten were found by asking *the running appliance* for something rather than
asking a component about itself. Three were disagreements between two modules about a name
(`entry.item` versus `item.status`), which no single-module test can see; one was a library's
behaviour rather than ours (`request.is_disconnected()` competing with `sse-starlette`'s own
listener for the single `http.disconnect` message); one was a probe reading two attributes that
do not exist, found by starting the composition root against a real database.

The eleventh is different in kind: `hx-swap="find .row-status"`, found while documenting the
vendored htmx, in a template no test had ever parsed. `tests/unit/test_templates.py` is the
guard — a text check, because the suite has no browser and cannot execute 50 kB of
minified htmx to find out whether a swap style is real. The front end’s contract is now
written down in [`docs/Developer/Frontend.md`](../docs/Developer/Frontend.md).

## Deviations, stated plainly

- **No Tailwind.** AIG 13 lists it; Encore ships 540 lines of hand-written CSS. An offline
  appliance cannot fetch a CDN, and a build step between a tarball and a running jukebox is a
  step that can be missing. Reversible without touching template structure. Recorded in
  ADR-012's consequences.
- **htmx's SSE extension is not used.** `encore-live.js` owns the one `EventSource`, so there is
  one reconnect policy and one resync path rather than one per region attribute.
- **`sse-starlette` is a dependency** for the response plumbing and disconnect detection only;
  the frames are ours.

## What the next phase inherits

1. **Admin (milestone 14)** — login, dashboard, playback controls, queue management, statistics,
   log tail, configuration summary, library info, plus `DELETE /api/v1/queue/{id}` and
   `POST /admin/library/reload`. The controls exist as services (`PlaybackService.pause/resume/
   skip/stop`, `QueueService.remove`) and are reachable only through the appliance thread; the
   routes are the new part, and they need the session machinery SAPRS 10.6 describes.
2. **`/api/v1/stats`** — `StatisticsService` is in the AIG's core list and is not implemented.
   The playback history it reads is already written (`runtime.db`).
3. **QR (`/qr`)** — deferred by decision, so that the poster encodes the URL the installer
   actually configured.
4. **Coverage floor** — raise `fail_under` deliberately, with the number in `docs/Developer/
   Testing.md`, rather than letting it drift up as a side effect.
5. **Shared regions in admin pages.** There is no `hx-boost` anywhere — every swap is an
   explicit attribute, which is worth keeping because a boosted link changes what the fragment
   behind it has to be. Admin will want the same two regions; the temptation to add a third
   `alerts`-shaped region rather than reuse `swap:alerts` should be resisted, since the fan-out
   is per region and the frame budget is the party's.
