# Current Phase

**Phase 5 — the administrative interface (AIG step 14): merged as
[d688d9d](https://github.com/clydepro/encore/commit/d688d9d) through
[PR #27](https://github.com/clydepro/encore/pull/27), closing issue
[#28](https://github.com/clydepro/encore/issues/28).** Phase 6 — the installer
(step 15) — is what comes next, and no work has started on it. Phases 1–4 are merged:
[PR #20](https://github.com/clydepro/encore/pull/20) (domain, bus, configuration),
[PR #22](https://github.com/clydepro/encore/pull/22) (repositories, Builder — closed #19),
[PR #24](https://github.com/clydepro/encore/pull/24) (search, playback, queue — closed #23) and
[PR #26](https://github.com/clydepro/encore/pull/26) (HTTP, HTMX, SSE — closed #25). Phase 5 sat
on `main` at `532e384` and needed nothing from an unmerged sibling.

One deviation from CONTRIBUTING §2, now historical: the merged branch
(`feat/27-administration-interface`, deleted) was named after the pull request because the work
started before issue #28 existed. The issue was opened afterwards so that
`tests/regression/test_issue_28_administration_interface.py` could cite something real, and
renaming the branch mid-flight would have orphaned the open pull request. The lesson for phase 6
is the ordering CONTRIBUTING already states: open the issue, then branch, then write code.

## Delivered

| AIG 14 requirement | Delivered as | Spec |
| ------------------ | ------------ | ---- |
| Login | `services/admin_service.py`, `api/auth.py`, `templates/admin/login.html` | SAPRS 10.8, ADR-013 |
| Dashboard | `controllers/admin.py`, `api/admin_views.py`, `templates/admin/{pages,panels}/dashboard.html` | SAPRS 8.8 |
| Playback controls | `POST /admin/playback/{pause,resume,skip,stop,start}` → `QueueService` | SAPRS 8.8, ADR-011 |
| Queue management | `GET /admin/queue`, `POST /admin/queue/{id}/remove`, `/admin/queue/clear`, `DELETE /api/v1/queue/{id}` | SAPRS 8.8, 10.2 |
| Health | `GET /admin/health` over the existing `HealthService` snapshot | SAPRS 11.9 |
| Statistics | `services/statistics_service.py`, `GET /admin/statistics`, `GET /api/v1/stats` | SAPRS 10.3, AIG 7 |
| Logs | `LogTail` in `services/logging_service.py`, `GET /admin/logs` | AEP 15-16 |
| Configuration summary | `GET /admin/configuration` over `redact(config.model_dump())` | SAPRS 12.3, 12.7 |
| Library information | `GET /admin/library`, `POST /admin/library/reload`, `LibraryStore.reload()` | SAPRS 6.10, 6.11, ADR-006 |

**The credential** is one Argon2id (or bcrypt) hash in `runtime.db`, and **the sessions** are
random tokens in memory: ADR-013 states why, and the two consequences it accepts — `Secure`
cannot be set on a plain-HTTP appliance, and a restart signs the operator out. There is no CSRF
token; the cookie is `HttpOnly` + `SameSite=Strict`, and a state-changing request whose `Origin`
names another site is refused. First-run setup is the same route as login, because a second,
rarely-exercised setup path is a worse security surface than one branch in a tested one.
`ENCORE_ADMIN_PASSWORD` seeds the credential for unattended installs and is then forgotten.

**Nothing under `/admin` is public except the door.** `controllers/admin.py` exports two routers
— `router` (login only) and `secure`, which carries `Depends(depend_admin)` at router level — so
forgetting to protect a new route is not a thing a contributor can do by accident.
`tests/integration/test_admin_interface.py` asserts it against `app.openapi()["paths"]`, which is
the flattened document rather than `app.routes`; FastAPI ≥ 0.140 stores included routers as
opaque nodes, so an enumeration written against `routes` would compare an empty set to itself and
pass forever. That one is in `tests/regression/test_issue_28_administration_interface.py`.

**Administrative actions are queue mutations.** Every control calls `QueueService` — never
`PlaybackService` — so ADR-011's single mutator holds for the operator as well as the guest, and
the guest's screen stays coherent when someone at the laptop touches the list. Removing what is
playing is a skip, and both `DELETE /api/v1/queue/{id}` and the admin route report which of the
two happened.

**No new SSE regions, no new facts.** `REGIONS` in `encore-live.js` is still `player` and
`alerts`, and the vocabulary is still the seven Phase 4 events. The admin refresh mechanism is
one line: the live script dispatches `encore:change` on `document.body` when a frame says the
appliance moved, and each admin region carries
`hx-trigger="encore:change from:body delay:250ms"`. Forty phones pay nothing for a laptop's
dashboard, nothing under `/admin` polls (asserted as markup text, because a test cannot watch a
network it did not build), and the trade is written down: an operator's panel can be a quarter of
a second behind the frame that caused it. `docs/Developer/Frontend.md` has the section.

**Screens exist twice.** `admin/pages/x.html` is the document, `admin/panels/x.html` is the
region, and both are rendered from one function in `api/admin_views.py`. A control's answer is
the region that asked, named by `?panel=`; a browser without JavaScript gets the same command
performed and a `303` instead. The bug this convention was written around is in the regression
file: the first draft branched on the medium *before* performing the action, so Pause redrew the
dashboard over a track that was still playing.

**A failure is answered in the caller's language, and the client is told so.** A tool gets the
JSON envelope, a person who typed a URL gets the sentence inside the page shell, and a panel gets
the region's own markup — which needed a rule added to `encore-live.js`, because htmx's default
`responseHandling` discards a 4xx or 5xx body before reading it, so every error fragment this
repository has produced since milestone 12 had been rendered by the server and thrown away by the
browser. The rule is narrow (error responses, HTML only) and one status is handled above it: a 401
carries `HX-Redirect` to the sign-in page with `?next=` back, because a region cannot sign itself
in, and a fragment answers with `HX-Reswap: innerHTML` so that a failure goes *into* a
self-refreshing region rather than replacing the thing that makes it refresh. `Retry-After` goes
with every 429, in the header as well as the body, because a wait that only appears in JSON is a
wait that gets ignored.

**Statistics is a service, not a page feature.** `StatisticsService` subscribes to the facts the
bus already carries and maps them to counters through `FACT_COUNTERS`, plus three request-side
counters (`searched`, `artist_viewed`, `album_viewed`) called where the guest context is
assembled — not inside `SearchService`, so the page, the fragment and the JSON route cannot count
a view three different ways. `start()` is idempotent and `close()` unsubscribes, because a
service that subscribes twice is a dashboard that doubles every number, and a `report()` is one
transaction so the page and `/api/v1/stats` cannot disagree.

**A library reload is one reopen of one handle.** `LibraryConnection.reopen()` opens the same
path again, runs the shape check on the *new* handle, and swaps only if it passes; object
identity is preserved, which is what makes the repositories and `SearchService` — holding
`LibraryStore.search` — see the change. `reload()` publishes `LibraryReloaded`, the only fact the
runtime emits about the library, and the previous library stays current if the file is
unreadable. A rebuild published mid-party is noticed by one click and reaches every guest screen.

**The log tail is a ring buffer, not a journal.** `LogTail` keeps 400 flattened records
(a retained `LogRecord` holds frames, and frames hold locals), redacted through the same
`record_fields` the formatters use. The page prints `journalctl -u encore` beside the filter
because the durable record is the journal's (AEP 16) and the appliance's interface must work from
a phone, where a shell does not.

## Numbers

1268 tests pass in the standard gate (1312 collected; 44 skipped for want of
`--run-slow`) and 1309 with the slow suites untraced — one skip in both runs, the
milestone 16 Party Simulation driver that is not written yet. Coverage of `encore/`
is 93.5%.

The new code is at the top of the range, which is what a phase with this many
failure paths should look like: `api/auth.py`, `api/errors.py`, `controllers/admin.py`
and `LogTail` at 100%, `services/statistics_service.py` at 97.0% and
`services/admin_service.py` at 99.4%. The last item is a branch, not a line: the
exit edge of the eviction loop in `_mint()`, which the sixteenth-laptop test does
exercise in one direction and would need two sessions with the same `expires_at`
to the microsecond to exercise in the other. That is a test for `timedelta`
resolution rather than for the rule, so it is not written.

What is *not* covered is worth naming rather than averaging away:
`repositories/runtime/admin_state.py` at 83.5%, where the uncovered lines are the
rejects a corrupt or hand-edited `admin_state` row would take you to (a hash that
does not parse, a password past the hasher's 4096-byte limit, a scheme parameter that
is not a number) plus the factory-reset `delete()` that nothing calls yet. Those
paths are the appliance saying *this file is not what I expect*, and they will be
exercised by milestone 15's installer tests, which are the first ones with a reason
to write a broken row on purpose.

`fail_under` remains 0 as set at bootstrap; raising it is a decision for the
installer PR, not a side effect of this one.

Reference p95s from the development machine (ARM64, untraced), all inside SAPRS 1.8's budgets:

| Budget | Target | Measured |
| ------ | ------ | -------- |
| Dashboard page | 200 ms | 16.5 ms |
| Queue page | 200 ms | 10.7 ms |
| Statistics page | 200 ms | 13.7 ms |
| Log page | 200 ms | 5.3 ms |
| Search page (unchanged) | 100 ms | 20.7 ms |
| Queue mutation (unchanged) | 50 ms | 37.6 ms |

The four administrative budgets are new `--run-slow` cases in
`tests/performance/test_http_latency.py`. An administrative page pays one cost a guest never
does — the session lookup — and it is a dictionary probe rather than a hash verification, which
is why `curl` in a setup script does not multiply the number of password hashes computed per
second. The guest numbers moved slightly between phases and are re-measured rather than quoted
from memory.

## Nine defects, sorted by how they were found

Six are pinned in `tests/regression/test_issue_28_administration_interface.py`; the rest are in
the suite whose name is in the last column.

| Kind | Defect | Pinned by |
| ---- | ------ | --------- |
| An assertion that asserted nothing | `app.routes` does not flatten `include_router` (FastAPI ≥ 0.140), so "nothing under `/admin` answers without a session" compared an empty set with itself | regression |
| | A reload test whose helper `copyfile`s onto the published path: the live handle *does* see a copy, so the test proved the opposite of what it claimed | regression |
| | A login refusal that distinguished "no credential yet" from "wrong password" — a free oracle about `admin_state` | regression |
| Found by asking the running appliance | `POST /admin/playback/{command}` branched on the medium before performing: the fragment said "Paused" above a track still playing, while the browser branch was correct | regression, integration |
| | `sqlite3.DatabaseError` escaping the library shape check, which turned the reload button into a 500 about this process instead of a 503 about that file | regression |
| | The lockout's page: its status was right and its sentence unreachable, because `RateLimitError` is a sibling of the exception the login route caught | regression, integration |
| | `_sentence()` in `controllers/admin.py` — five operator-facing sentences, defined and documented, never called, so the transport buttons answered themselves with the path segment | integration |
| Found by reading what we depend on | htmx's default `responseHandling` discards every 4xx and 5xx body: the error *fragments* this repository has produced since milestone 12 were rendered by the server and thrown away by the browser | regression |
| | `LibrarySearch`'s availability, which is a property of the file and was being treated as an object identity (`reload()` swapped it out from under `SearchService`) | unit (`test_library_reload.py`) |

The pattern from Phase 4 held, with one term added: nothing in that list was visible from a unit
test of the module it lived in, and the two worst were not visible from any test in this
repository at all — one lived in a browser's library and one in an `except` clause that looked
complete.

## Deviations, stated plainly

- **No `Secure` cookie.** The appliance serves plain HTTP on a LAN (SAPRS 13.1) and a `Secure`
  flag would make the interface unreachable rather than safer. ADR-013's consequences record it
  as a trade, and the mitigation is the one that works without TLS: `SameSite=Strict` plus the
  origin check plus one appliance-wide credential.
- **No admin SSE stream.** `EventSource` sends same-origin cookies, so an authenticated stream
  would have worked; what it would have cost is a second fan-out, a second queue per tab and a
  second latency budget for one laptop. `encore:change` + a self-refreshing region is htmx doing
  the same job. Consequence: the operator's panel can trail the guest's by a refresh.
- **No per-request rate limit, and no per-address failure table.** Failures are counted per
  appliance. A per-address table would be the guest-tracking ADR-007 forbids and would protect
  nothing on a network with one address that matters.
- **A pause announces nothing.** Playing, finishing, skipping, advancing, recovering, reloading
  and health are facts; pausing is not, and this phase did not extend ADR-004's vocabulary to
  cover it. A guest who asks is told (the fragment reads live state); a guest who is watching
  finds out at the next fact. Documented in the Administrator Guide rather than discovered.
- **`encore/services/__init__.py` does not re-export `StatisticsService`.** The module needs
  `StatisticKey` from the runtime repository's vocabulary, and importing it at package level
  would pull SQLAlchemy into every configuration and domain test. The package docstring says so.
- **`hx-confirm` entered the template vocabulary**, for one button. `tests/unit/test_templates.py`
  was widened for it, with the reason in the docstring: a native confirm is the cheapest way to
  guard the one irreversible administrative action, and the alternative was a modal component.

## What the next phase inherits

1. **Installer (milestone 15)** — `encore-install`, systemd units, `/etc/encore/config.yaml`,
   `ENCORE_ADMIN_PASSWORD` seeding (the mechanism exists, the docs and the flag plumbing around it
   do not), the `encore` user and layout of SAPRS 13.3, and `GET /qr`, which needs the URL the
   installer actually configured. `/admin/library`'s "publish a new library" instruction is the
   installer's job to make true.
2. **Party Simulation driver (milestone 16)** — `tests/party_simulation/test_admin_in_the_room.py`
   added the operator to the shape of a party (removals, clears, skips, a pause, one ceiling
   ledger that still closes); the 15,000-song corpus, hundreds of guests and HTTP-fronted clients
   are still the driver's.
3. **Coverage floor** — raise `fail_under` deliberately, with the number in
   `docs/Developer/Testing.md`, rather than letting it drift up as a side effect.
4. **v1.1 statistics counters** — `StatisticKey` deliberately does not carry peak concurrency,
   distinct-device counts, crossfade faults or per-hour histograms; `test_statistics_service.py`
   asserts their absence so adding them is a decision with a test attached.
5. **`GET /admin/sessions`?** — the dashboard prints how many sessions are open. A page that lists
   them would say nothing an operator can act on (there are no addresses to show), which is the
   reason it was not built rather than an omission to fix quietly.
