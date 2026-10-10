# Handoff — after AIG steps 11, 12 and 13 (FastAPI, HTMX, SSE)

For whoever picks this up next. Read with
[`current-phase.md`](current-phase.md) (what was done) and
[`context/milestones.md`](context/milestones.md) (what exists). Precedence is unchanged:
task request → SAPRS → AIG → ADRs → AEP (AEP 2). This page is a pointer, not an authority,
and it will be wrong faster than the SAPRS is.

State as of this writing: **Phases 1–3 are merged (#20, #22, #24); this branch is the fourth**
and sits on `main` at `4d30b42` as `feat/25-runtime-server-htmx-sse`, for issue
[#25](https://github.com/clydepro/encore/issues/25). The appliance runs: it serves pages,
fragments, a versioned JSON API and an SSE stream over the composed graph, against real
databases and a fake mpv. 1109 tests pass in the standard gate, 1146 with the slow
suites untraced, 92.7% coverage, `ruff` and strict `mypy` clean. **Nothing here has met a browser on a
phone** — the HTML and the stream are tested through an ASGI driver and an htmx double, not
through WebKit; that gap is milestone 14's to close or yours to volunteer.

Steps 14–17 have no issues yet. Open yours before branching (CONTRIBUTING §2).

## Start here

1. **`docs/adr/ADR-012-*.md`** — the phase's premise in ten minutes: one thread owns SQLite and
   mpv, the event loop belongs to HTTP, and every service call crosses that line through
   `call_async`. Everything surprising about the code below falls out of it.
2. **`encore/api/deps.py`** — 130 lines, and the boundary is one function (`read`). If you
   understand why a handler cannot hold a session, you understand the milestone.
3. **`encore/services/sse_publisher.py`** — the frame vocabulary, the per-client bounded queue,
   the `snapshot`-then-facts ordering and the `resync` that replaces replay. Its docstrings say
   which SAPRS clause each decision answers.
4. **`encore/api/fragments.py`** — the region map. Facts and HTML are different frames, rendered
   once per fact for all screens; `_REGIONS_BY_NAME` is written by class name so a ninth event
   is *seen* by a reviewer rather than discovered by a guest.
5. **`apps/server/appliance.py`** — the composition root, and the order inside `start()`. It is
   also the reason `tests/support/appliances.py` exists: one call, the real graph.

Then `tests/integration/test_server_app.py`, which is this phase's story told as assertions.

## Things that will surprise you

- **`httpx.ASGITransport` cannot express a held-open response** — it accumulates
  `body_parts` and returns them at the end. That is why `tests/support/http.py::Stream` drives
  the ASGI app by hand, and why the SSE tests are readable at all. `running()`/`started()` use
  `httpx.AsyncClient` for ordinary requests; do not "simplify" `Stream` away.
- **`fastapi.testclient` is not used anywhere.** `filterwarnings = ["error"]` plus Starlette's
  deprecation warning on `TestClient` means importing it fails the suite at collection. The
  async client is the workaround and it is better anyway: one client, streaming supported.
- **A browser is `Accept: text/html`; `*/*` is a tool.** `curl` gets JSON from the HTML routes.
  It was a deliberate decision, it is written in `docs/api/README.md`, and it will look like a
  bug to whoever types `curl localhost:8080/` first.
- **The frame names on the wire are Python class names** (`SongQueued`, `QueueAdvanced`) because
  `EVENT_NAMES` derives them from `EVENT_VOCABULARY`. Transport frames are lowercase
  (`snapshot`, `progress`, `resync`) and region frames are `swap:player` / `swap:alerts`. Three
  namespaces in one stream, distinguished by shape on purpose.
- **Artwork misses are two answers.** Nothing depicted → `200` SVG placeholder; a row naming a
  file the cache lost → `404`. `LibraryService._file` returns the `ArtworkFile` for a vanished
  file so the caller can tell `exists=False` from "no reference", and that distinction is the
  install fault SAPRS 12 wants visible.
- **`QueueService.start()` restores and stays silent.** A restarted appliance does not make
  noise before a guest reaches the page. `tests/e2e/test_guest_journey.py` asserts it; if you
  "fix" it, you have changed a decision, not a bug.
- **The core-import guardrail runs in a child interpreter.** `test_core_foundation.py` used to
  inspect `sys.modules`, which became order-dependent the moment HTTP tests pulled Starlette into
  the same process. A subprocess is the honest check.

## Known loose ends

- **Tailwind is not in the build** (AIG 13 lists it). Hand-written CSS, vendored htmx, recorded
  in ADR-012's consequences. Cheap to reverse, deliberately not cheap to forget.
- **`StatisticsService` is not implemented** — it is in AIG 7's list and `runtime.db` already
  writes the history it would read. `docs/api/README.md` does not document a `/stats` route
  because there is nothing to document.
- **No `docs/api/openapi.json` is committed.** FastAPI generates it at `/openapi.json` on a
  running appliance, which is the version that cannot go stale; publishing a copy is a
  `scripts/` job for the docs workflow (milestone 17), and needs a decision about whether CI
  fails when the committed file drifts.
- **`fail_under` is still 0.** Actual coverage is 92.7%. Raise it on purpose, with the number
  recorded in `docs/Developer/Testing.md`, not as a diff in someone else's PR.
- **Queue POST p95 is 34.6 ms with a 143 ms worst case** in a 200-sample run on the dev box. The
  budget is met and the tail is mpv's `loadfile` plus a WAL checkpoint; if milestone 15's Pi 4
  testing says otherwise, the number to move is the timeout in `Stream.next`, not the budget.
- **The `progress` frame is 1 Hz regardless of need** (`playback.progress_interval_seconds`).
  Right for a progress bar, wasteful for 40 idle screens; an admin "pause everything" toggle for
  it belongs with milestone 14's configuration summary, not here.

## If you are doing the administrative interface

- The services already exist. `PlaybackService.pause/resume/skip/stop` and
  `QueueService.remove` are callable today from the appliance thread; the routes are new work,
  not the wiring.
- `DELETE /api/v1/queue/{id}` and `POST /admin/library/reload` are the two paths
  `docs/api/README.md` promises and does not have. `LibraryService` has no reload method yet —
  ADR-006's library swap is a store reopen plus a `LibraryReloaded`, and the event already
  exists with a region mapping waiting for it.
- Sessions: SAPRS 10.6, and the admin routes are the first thing in Encore with state that is
  not in a database. Do not put the session in `runtime.db` without an ADR — the appliance has
  one writer thread by design and a session wants to be a cookie.
- The `/api/v1/health` route is already the admin health payload minus the presentation; reuse
  `HealthSnapshot` rather than re-reading components in the controller.

## If you are doing the installer

- `scripts/run-server.sh` is the unit `systemd` calls; the config path is
  `/etc/encore/config.yaml` by convention and `--config` overrides it.
- The server refuses to start, loudly, when `library.db` is missing or is not an Encore library
  (`exit 2`, `EX_CONFIG`-style `78` for a store it could not open). An installer that reports
  success against an unbootable appliance is the failure this code exists to make impossible.

## How to run this phase's work

```bash
scripts/check.sh              # 1109 tests, coverage, lint, types, docs links
scripts/check.sh --slow       # + performance, real-mpv, e2e, untraced
uv run pytest tests/e2e -q    # the guest journey alone
./scripts/run-server.sh       # needs a built library; see apps/server/README.md
```
