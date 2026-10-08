# Handoff — after Phase 1 (Core Foundation)

For whoever picks this up next. Read with
[`current-phase.md`](current-phase.md) (what was done) and
[`context/milestones.md`](context/milestones.md) (what exists). Precedence is
unchanged: task request → SAPRS → AIG → ADRs → AEP (AEP 2). This page is a
pointer, not an authority, and it will be wrong faster than the SAPRS is.

## Start here

Work on **[issue #19](https://github.com/clydepro/encore/issues/19)**: the two
schemas and `encore/repositories/`. It is blocked by nothing and blocks the
Library Builder, search, queue and the runtime database.

Before writing code, decide the two things #19 names — SQLAlchemy's role in
`runtime.db`, and `runtime.db` versioning — and put each in an ADR if the answer
is not "the standard library is sufficient" (AEP 10). Both questions have a
disciplined answer and several undisciplined ones, and the undisciplined ones look
fine until a second contributor arrives.

Branch it, per `CONTRIBUTING.md` §7. `main` rejects direct pushes.

## Ten minutes of orientation

```bash
scripts/bootstrap.sh          # once
scripts/check.sh              # the gate CI runs; ~8 s
uv run pytest tests/unit/test_event_bus.py -q --no-cov   # the most alive part
```

The four packages to read, in the order that makes sense:

1. `encore/domain/` — SAPRS Ch. 4 as frozen dataclasses. Fifteen minutes, and it
   is the vocabulary everything else speaks.
2. `encore/events/` — `base.py` first (three properties of a fact), then `bus.py`.
   The bus docstring's "Delivery model" section is the shortest description of
   how Encore's parts talk.
3. `encore/config/` — `models.py` mirrors `examples/config.yaml` one class per
   section; `service.py` is the loader and the diagnostic.
4. `encore/services/container.py` — 97 lines, and the only place that knows how
   the core is built.

## Things that will surprise you

- **`@dataclass(slots=True)` and zero-argument `super()` do not mix.** A subclass
  calling `super().__post_init__()` raises `TypeError` on first *publication*, not
  at import. Every event uses `Event.__post_init__(self)`. See
  `encore/events/base.py`.
- **A dataclass field named `logging` shadows the module** for the rest of that
  class body, so `CoreServices.logger` is annotated `Logger`, not
  `logging.Logger`. See the comment in `encore/services/container.py`.
- **`publish()` does not await coroutine handlers.** It schedules them, so a slow
  subscriber cannot delay playback (SAPRS 11.4). Tests that need completion call
  `await bus.drain()`, or use `publish_async()`. A coroutine handler published with
  no running loop is *skipped and logged*, not run late.
- **`EventCycleError` escapes while every other handler error is swallowed.** That
  asymmetry is deliberate: SAPRS 11.3 protects a publication from a subscriber,
  not from the wiring being wrong. A cycle would otherwise exhaust the stack.
- **`allow_duplicates: false` fails startup.** SAPRS 8.3 is a rule, not a
  preference, and the configuration layer refuses to look like a knob for it
  (SAPRS 12.3). The same guard sits on the model, so `model_copy(update=…)` cannot
  bypass it — `EncoreConfig` revalidates instances.
- **An empty section (`audio:` with nothing under it) means "defaults"**, not
  "disabled". Normalized in the loader, with the reason in a comment.
- **Secrets are withheld by the log formatter**, not by callers, because a service
  forgetting to redact is the normal case. `record_fields()` is the single answer
  to "what did we log?". An `extra` key that collides with the four envelope keys
  is kept under an `extra_` prefix rather than dropped or allowed to overwrite.
- **`assert` in production code is not a validation strategy.** Every invariant in
  this phase raises `ValueError` from `__post_init__` or a Pydantic validator.

## What is deliberately not here

No HTTP, no database, no player, no templates, no installer. If a task looks like
it needs one of those, it is a later step and the honest answer is that this phase
prepared the vocabulary for it and nothing else.

## Claims made in Phase 1 that later code must keep true

- The domain imports nothing from `encore/` except `encore.utilities`. Checked.
- No service instance is reachable as a module global. Checked.
- No entity or event carries guest identity. Checked by field name, so
  `source_ip` fails CI.
- Configuration cannot write anything. Checked by AST scan.
- `library.db` and `runtime.db` are different files. Checked at load; #19 must
  make it mean something by opening one read-only.
- Timestamps are timezone-aware UTC. Checked at construction; #19 must not let a
  naive value into a row.

The first four are tests in `tests/unit/test_architecture_guardrails.py`. The
last two are only half-enforced until the databases exist — that is #19's
acceptance criteria, not a gap in this phase.

## Known loose ends

- No tracking issue existed for steps 2–4, so the CHANGELOG entry and the commit
  are the record of what was done. #19 is the first issue in the sequence that
  `CONTRIBUTING.md` describes.
- The `slots`/`super()` trap deserves a file in `tests/regression/` per AEP 13,
  but that suite is one file per issue number and this was found while writing
  code, not reported. The guard is in `tests/unit/test_events.py` and labelled as
  such; open a `chore` issue and move it if tidiness matters.
- `examples/config.yaml` is asserted equal to the model defaults on every commit.
  If you change a default, that file changes in the same commit — the test tells
  you, the docs will not.
