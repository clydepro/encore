# ADR-012: One Appliance Thread; The Event Loop Belongs to HTTP

## Status

Accepted

## Date

2026-10-10

## Context

Milestones 11–13 (AIG 21) put an HTTP server in front of services that were
written, tested and reasoned about as single-threaded. Two facts about what
already exists decide the shape of that server, and neither is visible from the
SAPRS.

**The Event Bus is synchronous** (ADR-004), deliberately: `QueueService.enqueue()`
returns "you are playing now" as a true statement rather than an optimistic one,
and ADR-011's command/notify split only works if a handler finishes before the
publication returns. The consequence for a web tier is that a call made from an
HTTP handler does not stop at the service boundary — it runs the queue advance,
which calls `PlaybackService.play()`, which writes to mpv's IPC socket and waits,
which opens a SQLite transaction, all inline on the caller's thread.

**mpv's IPC is one socket with no lock.** `encore/playback/ipc.py` holds a single
read buffer, a single monotonically-increasing `request_id` counter and one
blocking socket. Replies are matched by id, so a second thread waiting for its own
reply will read the first thread's, discard it (it is not an event), and block
until its timeout expires. Two threads issuing commands therefore do not merely
race: they lose each other's answers, and the symptom is a `MpvTimeoutError` on an
engine that is perfectly healthy. Phase 3's own review of the async option left a
note in `ai/current-phase.md`: making the bus asynchronous is a change to argue
about in an ADR before writing it.

Meanwhile SAPRS 11.4 asks the opposite of the same process: long-running work must
not block critical playback, and SSE connections are held open for the life of a
guest's phone — dozens of them, at a party, on one 4 GB board.

So the naive readings both fail. `async def` handlers calling services directly put
mpv's load latency on the event loop that feeds every `EventSource` in the room.
Sync handlers let Starlette run them on a threadpool of forty workers, which is
forty potential callers of one unguarded socket.

## Decision

**The appliance has exactly one thread that owns its services, and it is not the
event loop.**

- `apps/server/` starts a single-worker dispatcher (the *appliance thread*). Every
  call that can reach playback, the Event Bus or `runtime.db` — queueing, an
  administrative command, a fragment that reads the queue, a progress poll — is
  submitted to that thread and awaited by whoever asked. One worker means
  submission order is execution order, so the FIFO the queue promises is the FIFO
  the thread actually runs.
- The event loop does HTTP and SSE and nothing else: parsing requests, rendering
  templates, holding connections open. It never touches mpv or a session.
- Fan-out crosses the boundary in the other direction with
  `loop.call_soon_threadsafe`. `EventBus` handlers registered by the SSE publisher
  are synchronous and return in microseconds — they put a rendered fact on a
  bounded per-client queue and get out, which is what keeps SAPRS 11.3's "one
  subscriber failing must not stop the others" cheap enough to be true.
- The periodic `tick()` (SAPRS 7.5's ~1 Hz progress observation, the engine-death
  check, and health aggregation) is a background task on the loop that submits its
  work to the appliance thread. It is the same call the request path makes, so it
  cannot interleave with it.
- A request that plays a song publishes and waits; it does not poll. There is no
  second, implicit path into the services anywhere in the tree — the composition
  root builds the graph once, at startup, in SAPRS 11.7's order.

**What this is not:** a thread-safe runtime. Nothing in `encore/` gains a lock, and
nothing in `encore/` learns about `asyncio`. The invariant is maintained by
admitting exactly one caller, which is the same argument ADR-005 makes for polling
instead of subscribing to mpv's event stream: determinism bought with a constraint
rather than with a mutex.

## Consequences

Positive:

- Phase 3's services are unchanged. No lock is added to `JsonIpc`, no handler
  becomes a coroutine, and the queue's `_advancing` re-entrancy guard stays a
  single-thread argument rather than becoming an atomic flag nobody can reason
  about.
- mpv latency, SQLite write latency and WAL checkpoints stop being guest-visible
  as a stalled page: they queue behind one another on a thread whose only job is
  serialization, while the loop keeps streaming to everyone else.
- `runtime.db` gets one writer thread for free, which is the calmest configuration
  SQLite's WAL will do and the reason `busy_timeout` almost never fires here.
- The whole runtime is testable the way it runs. An integration test can build the
  same graph with the same single-thread property and drive it with an HTTP client,
  and the `MockMpv` double remains faithful rather than accidentally-threaded.

Costs:

- **Throughput is bounded by one thread.** A party of 40 guests shares a serialized
  service path. That is the right trade for a machine whose steady-state work is
  one mpv process and a queue of 200 rows, and the wrong trade for anything else;
  if it ever stops being enough, the fix is a queue of *work items* with typed
  handlers, not a second thread holding the same socket, and that is this ADR's
  replacement.
- **Every service call is now potentially blocking-for-a-while**, so an HTTP
  handler must never assume the answer is immediate. A request that times out while
  the appliance thread sits in an mpv round trip is a real user-visible failure
  mode; the budget in SAPRS 1.8 (queue < 50 ms) is what keeps this honest, and it is
  measured rather than asserted.
- A handler that calls into a service from the loop by mistake — a `def` route
  Starlette runs on a worker thread, an `await`-less call inside an `async`
  function — is a bug this design cannot make unrepresentable. It is caught by
  `tests/unit/test_architecture_guardrails.py`'s import rules and by the appliance
  thread's own "wrong thread" assertion, not by the type system.
- SSE fan-out now has a real drop policy (a slow client's queue fills), because a
  thread boundary made "unbounded buffering" a memory leak instead of a lie.
- **The front end deviates from AIG 13's stack list in one respect.** AIG 13 names
  Tailwind CSS; Encore ships hand-written CSS in
  `encore/static/css/encore.css` and vendors htmx at
  `encore/static/vendor/htmx/htmx.min.js`. The reason is SAPRS 13's offline rule
  rather than a preference about class names: Tailwind's utility CSS is produced by
  a build step over templates, and a build step in the path to a running jukebox is
  a step that can be missing on a Pi that was installed from a release tarball.
  Vendoring htmx is the same argument settled the same way — no CDN at runtime. A
  Tailwind build can be introduced later without touching a template's structure,
  which is the property that makes the deviation cheap to reverse.

## Alternatives considered

- **`async` services and an async Event Bus.** Rejected: it is a rewrite of
  ADR-011's premise, and `ai/current-phase.md` names it as the change to argue
  before writing. It also buys nothing here — the work being scheduled is blocking
  I/O against a subprocess and a file, not network fan-out.
- **Locks inside `JsonIpc` and `RuntimeStore`, sync routes on the threadpool.**
  Rejected: it makes a correct-by-construction property correct-by-audit. Every
  future command added to mpv would need the lock remembered, and a lock on the
  socket does not make a queue advance and a progress poll agree about what the
  engine is doing mid-command.
- **Run everything on the loop and make playback non-blocking** (an `async` mpv
  client). Rejected: phase 3's IPC is a synchronous, tested, understood component
  that has met a real mpv. Replacing it to avoid one thread optimises the wrong
  thing.
- **A process per guest connection.** Rejected: mpv is one process with one set of
  speakers, and the queue is shared state.
- **`asyncio.to_thread` per request.** Rejected: unbounded workers, and the
  composition of "unbounded" with "one unguarded socket" is exactly the failure
  described in Context.
- **`sse-starlette`'s own event-stream plumbing, or htmx's SSE extension, as the
  browser half.** Rejected on the offline rule rather than on taste: an appliance
  in a basement with no DNS cannot fetch a CDN at runtime, so htmx is vendored and
  the bridge in `encore/static/js/encore-live.js` is ours. That also means the
  SSE connection lives in one place with one reconnect policy, instead of one per
  region attribute — which is the difference between a party where every screen
  redrew from the same fact and a party where two panels disagree.

## Verification

- `tests/integration/test_server_app.py` drives the graph over HTTP with the real
  Builder library and the `MockMpv` double — the same wiring
  `tests/integration/test_queue_playback_and_search.py` established, now with a
  request on the other side of it.
- `tests/unit/test_appliance_thread.py` asserts the appliance thread answers on its
  own thread: a call made from the loop runs elsewhere, calls made concurrently
  serialize in submission order, and a service that reached for the loop directly
  is named. `worker_ident` exists so the assertion can be made about a served
  request — `test_the_guardrail_reports_the_thread_that_did_the_work` — rather than
  about a helper the application may stop calling.
- `tests/performance/test_http_latency.py` measures the two budgets this milestone
  makes real — navigation < 200 ms and SSE propagation < 1 s (SAPRS 1.8) — over the
  composed app rather than over services, so the number includes the template,
  the thread hand-off and the fan-out. On the ARM64 development machine a guest
  page is 9.6 ms p95 and one fact reaches 40 screens in 8.7 ms wall.

## Related

- ADR-004 (synchronous in-process bus) — the premise this builds on.
- ADR-011 (queue commands playback) — the call that runs on the appliance thread.
- ADR-005 (mpv IPC, polling at ~1 Hz) — the `tick()` boundary in the decision.
- ADR-002 (HTMX instead of a SPA) — why the loop's only client-side job is a
  fragment swap and an `EventSource`.
- SAPRS 11.4, 11.7, 11.8, 11.9, 1.8.
