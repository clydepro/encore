# Handoff — after AIG step 14 (the administrative interface)

For whoever picks this up next. Read with
[`current-phase.md`](current-phase.md) (what was done) and
[`context/milestones.md`](context/milestones.md) (what exists). Precedence is unchanged: task
request → SAPRS → AIG → ADRs → AEP (AEP 2). This page is a pointer, not an authority, and it will
be wrong faster than the SAPRS is.

State as of this writing: **Phases 1–5 are merged (#20, #22, #24, #26, #27).** Phase 5 landed as
[d688d9d](https://github.com/clydepro/encore/commit/d688d9d) for issue
[#28](https://github.com/clydepro/encore/issues/28). (The branch is named for the pull request
rather than the issue, which breaks CONTRIBUTING §2; `ai/current-phase.md` says why and why it
was not renamed.) The appliance has two audiences now: a guest
queues a song on a phone, and an operator signs in on a laptop, pauses the room, takes a request
out of the middle of the queue, reads the log tail, notices that the library was rebuilt and picks
the new one up without a restart. 1268 tests pass in the standard gate and 1311 with the slow
suites untraced, 93.5% coverage, `ruff` and strict `mypy` clean
across `encore/`, `apps/`, `tests/` and `tools/`.

**Nothing here has met a browser on a phone.** That sentence was written in the last handoff about
the guest interface and it is still true, and milestone 14 was the wrong place to fix it: the
administrative interface is the one that *wants* a real browser, because the two things only a
browser can tell you are whether `SameSite=Strict` survives a redirect and whether a native
confirm dialog on the queue-clear button reads as help or as friction. Both are asserted as markup
here, which is the honest substitute and not a proof.

Step 15 (the installer) has no issue yet. Open it before branching (CONTRIBUTING §2).

## Start here

1. **`docs/adr/ADR-013-*.md`** — the phase's premise in eight minutes: the credential lives in
   `runtime.db`, the sessions live in memory, and the two consequences that decision buys (a
   restart signs you out; `Secure` cannot be set on a plain-HTTP appliance) are stated rather than
   softened. It also names what was rejected: a token file, sessions in the database, and a CSRF
   token.
2. **`encore/services/admin_service.py`** — the whole of the authentication policy, with no HTTP
   in it. Its two most important lines are the one that hashes and the one that raises the same
   message for a wrong password and for no credential.
3. **`encore/api/auth.py`** — the cookie and the origin check, and the dependency every protected
   route reaches. Twelve rules in eighty lines; `tests/unit/test_api_auth.py` is the version of
   that sentence you can check.
4. **`encore/controllers/admin.py`** — the router split (`router` public, `secure` protected at
   router level), the page/panel duality, and the `?panel=` convention that lets one action
   handler answer ten screens.
5. **`docs/Developer/Frontend.md`'s "Administrative pages"** — the refresh mechanism, which is one
   dispatched event and a trigger attribute, and the reason there is no second SSE stream.

Then `tests/integration/test_admin_interface.py`, which is this phase told as assertions.

## Things that will surprise you

- **Nothing under `/admin` is public except the door, and the test that proves it reads the
  OpenAPI document rather than the route table.** `controllers/admin.py` exports two routers so
  that forgetting to protect a route is not a thing a contributor can do; FastAPI ≥ 0.140 keeps an
  `include_router` as one opaque node, so the obvious way to enumerate routes silently finds
  nothing. That is in `tests/regression/test_issue_28_administration_interface.py`, and it is the
  kind of bug that looks like coverage.
- **An action's response is the region that asked, named by `?panel=`.** The alternative was each
  page knowing which other pages its buttons affect. A no-JavaScript browser gets a `303` and the
  same command performed — performing it *before* choosing the markup is the fix for this phase's
  worst bug, and the regression file explains why the browser branch passing hid it.
- **A pause announces nothing.** Playing, finishing, skipping, advancing, recovering, reloading
  and health are facts (`encore/events/`); pausing is not, and this phase deliberately did not
  extend ADR-004's vocabulary to cover it. So a guest who *asks* sees `paused` and a guest who is
  *watching* sees a bar that stopped moving. The Administrator Guide says so, in the section beside
  the button.
- **The statistics service is not imported by `encore/services/__init__.py`.** It needs
  `StatisticKey` from the runtime repository's vocabulary, and importing it at package level pulls
  SQLAlchemy into every configuration and domain test. The package docstring says so; do not
  "tidy" the import back in.
- **`library.db` really cannot be written, and a reload proves it rather than asserting it.**
  `LibraryConnection` opens with `PRAGMA query_only` on an `immutable=1` URI (ADR-009), and
  `reload()` is a reopen of that same handle — verification on the new handle, swap only on
  success, object identity preserved so the repositories and `SearchService` notice the change.
  Two consequences worth knowing before touching either: a failed reload leaves the running
  library serving, and `LibraryStore.search`'s FTS verdict is updated *in place* because replacing
  the object would leave search answering from a library nobody has.
- **The log page is a ring buffer with a caption.** 400 flattened records in memory, redacted by
  the same `record_fields` the journal formatters use, and no tracebacks — `LogRecord` holds
  `exc_info`, which holds frames, which holds locals. The page prints `journalctl -u encore`
  because the durable record is the journal's (AEP 16).
- **Counters are incremented where the request is assembled**, in `api/views.py`, not inside
  `SearchService` or `LibraryService`. Three routes share one context builder, and a counter in
  the service would be a counter that counts a search twice on a page and not at all on the API.
- **A failure is answered in the caller's own language, and the bridge makes that visible.**
  `errors.respond()` gives a tool the envelope, a person who typed a URL the page shell, and an
  HTMX swap a fragment — the same status and code in all three. The third of those needed a rule
  in `encore-live.js`, because htmx's default `responseHandling` throws away a 4xx or 5xx body
  before reading it, which had been silently unwriting every error fragment this repository has
  ever sent. An expired administrative session is the one case that must not be *swapped*, so it
  carries `HX-Redirect` too and the tab ends up at the sign-in page with `?next=` back. Both are
  pinned in the regression file, including the vendored default.
- **A locked appliance says when, twice.** `details.retry_after_seconds` for a client that parses
  the envelope and a `Retry-After` header for a proxy, a `curl --retry` or anything generated from
  the OpenAPI document. The lockout is refused before any hashing, and it is counted per appliance
  rather than per address (ADR-007), so neither the timing nor the status tells a guesser whether
  they were close.
- **`assert response.status_code == 303` on an admin `GET` is what "not signed in" looks like in a
  browser.** SAPRS 10.7 lists 401 and 303 as answers to different callers; the same failure answers
  a person with a door and a tool with a code, and the split is in `errors.respond()`.

## Known loose ends

- **The first-run password is set over HTTP.** `AdminService.setup()` warns in the log when it
  runs from a request, because the intended path for an install with an operator present is
  `ENCORE_ADMIN_PASSWORD` at first boot. The warning is deliberate; the alternative was a setup
  token, which is a second credential system.
- **No `docs/api/openapi.json` is committed.** FastAPI generates it at `/openapi.json`, which is
  the version that cannot go stale. Publishing a copy is a `scripts/` job and needs a decision
  about whether CI fails when the committed file drifts.
- **`fail_under` is still 0.** Actual coverage is 93.5%. Raise it on purpose, with the number in
  `docs/Developer/Testing.md`, not as a diff in someone else's PR.
- **There is no session list page.** The dashboard says how many sessions are open; a page that
  listed them could say nothing more, because there are no addresses to show (ADR-007). If an
  operator ever needs to kick one laptop out, the answer today is the password change, which ends
  all sessions.
- **`/admin/logs` reads memory, so it shows nothing from before a restart.** Correct for a tail and
  confusing at 2 a.m.; the page says which it is, and `journalctl -u encore` is printed beside it.
- **The `encore:change` refresh is a quarter-of-a-second coarse**, with htmx's `delay:250ms`
  collapsing a skip's two frames into one repaint. A dashboard left open on a tablet therefore
  redraws once per *burst*, which is the behaviour we want and which no test measures as latency —
  the admin budgets in `tests/performance/test_http_latency.py` are page renders, not refreshes.
- **Queue POST p95 is 37.6 ms and administrative pages are 5–17 ms** on the development machine,
  untraced. The numbers in the docs are from this phase's runs; if milestone 15's Pi 4 testing
  says otherwise, fix the numbers and the sentence beside them, not the budget.

## If you are doing the installer

- **`ENCORE_ADMIN_PASSWORD` is already honoured** — `apps/server/appliance.py` seeds the credential
  on first start and logs once if it was already set. What the installer must do is write it into
  the unit's environment (SAPRS 13.11: `ENCORE_HOME`, `ENCORE_CONFIG`, `XDG_CACHE_HOME`) and say in
  the manual steps that the password is set at install time rather than at first sign-in.
- **`GET /qr`** is the installer's, not this page's: the poster has to encode the URL the install
  actually configured (`server.domain` or the `jukebox.local` mDNS name), and guessing it here
  would print a QR code to a host that does not exist. The guest-facing pages have no admin link,
  and the QR is the only address an operator will ever hand out.
- **`library.db` publishing is the installer's other job**: `/admin/library` tells the operator to
  publish a new library and gives them the reload button, but nothing in the Server writes
  `library.db` or runs the Builder (ADR-001). The gap between those two sentences is a chapter of
  the Administrator Guide and one flag of `encore-install`.
- **The server refuses to start, loudly**, when `library.db` is missing or is not an Encore
  library. An installer that reports success against an unbootable appliance is the failure that
  code exists to make impossible.
- **Migrations are forward-only and run at startup** (`runtime.db`), including the
  `admin_state` table this phase added. An installer that offers a rollback needs to know that the
  appliance does not have one.

## If you are doing the Party Simulation driver

- `tests/party_simulation/test_admin_in_the_room.py` added the operator to the shape of a party:
  removals (waiting and current), clears, skips, a pause-and-resume, and a ledger that still
  closes. The conservation arithmetic there is the part worth keeping when the driver grows — an
  item that ends twice, or ends never, breaks the sum rather than a screenshot.
- `profile.library_songs == 15_000` is asserted by `test_party_simulation.py` and is still not
  reachable: the fixture corpus is seven tracks. The driver's first job is a generator fast enough
  to be run in CI at that size, or a documented skip that says which half is measured.
- HTTP in the simulation is now possible: `tests/support/http.py::started()` drives the shipped
  composition root and `Stream` holds `/events` open. A guest that queues over HTTP and an
  operator that removes over HTTP share one appliance thread, so a simulation that runs both is
  testing ADR-012's serialization rather than a threaded free-for-all — which is the honest thing
  to measure.

## How to run this phase's work

```bash
scripts/check.sh              # 1268 tests, coverage, lint, types, docs links
scripts/check.sh --slow       # + performance, real-mpv, e2e, untraced
uv run pytest tests/integration/test_admin_interface.py -q    # the admin surface, end to end
uv run pytest tests/e2e/test_admin_journey.py -q             # an operator's day, in order
uv run pytest tests/unit/test_admin_service.py \
             tests/unit/test_api_auth.py \
             tests/unit/test_statistics_service.py \
             tests/unit/test_library_reload.py -q            # the four new units
./scripts/run-server.sh       # needs a built library; see apps/server/README.md
```

There is now a reason to start it by hand: `http://localhost:8080/admin` on a fresh appliance
asks you to set a password, and everything after that is a page.
