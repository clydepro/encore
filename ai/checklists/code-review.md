# Code Review Checklist (human)

The AEP 25/26 list, expressed as review actions. Reviewers use this; the AI pass
in `ai/prompts/code-review.md` is a warm-up, not a substitute.

## Architecture

- [ ] Owning service is correct; one responsibility, no new coupling
      (SAPRS 11.6).
- [ ] Event Bus used for cross-service facts; no handler-to-handler calls
      (ADR-004).
- [ ] No FastAPI/Jinja/HTTP concepts in `encore/domain`, `services`, `events`,
      `playback` (SAPRS 11.10).
- [ ] No SQL outside `encore/repositories`; no business rules inside them.
- [ ] No runtime write to `library.db`; no Builder import of playback.
- [ ] Templates contain presentation only; no decisions in Jinja.
- [ ] If a boundary moved, an ADR moved with it.

## Correctness

- [ ] Acceptance criteria in the issue demonstrated by a test, not by prose.
- [ ] Failure and recovery paths handled, especially mpv, SSE and missing files
      (SAPRS 7.6, 11.9).
- [ ] Concurrency and ordering: queue transitions, event publication order.
- [ ] Duplicates and edge cases in the queue (SAPRS 8).

## Tests

- [ ] Unit + integration coverage proportional to risk; API/UI tests where
      observable (SAPRS 14.6–14.8).
- [ ] A test that would fail without the change.
- [ ] No test needing network, real mpv or audio hardware.
- [ ] Regression test for any fixed defect (`ai/checklists/regression.md`).

## Quality

- [ ] `scripts/check.sh` green locally; CI green.
- [ ] Type hints complete; no `Any` without reason (AIG 17).
- [ ] Naming intent-revealing; no magic values; no dead code.
- [ ] Docstrings for public objects; comments explain why (SAPRS 15.7).

## Operations

- [ ] Structured logs at the right level; no secrets, cookies or personal data
      (AEP 16).
- [ ] Errors actionable and non-revealing (AEP 15).
- [ ] Configuration handled as installation-time only (SAPRS 12, ADR-008).
- [ ] Health/statistics reflect the new subsystem, or an issue is filed.

## Performance

- [ ] Relevant SAPRS 1.8 budget measured, or explicitly deferred with a
      benchmark task.
- [ ] No N+1 queries, unbounded in-memory queues, or blocking calls on the
      playback path.

## Scope and hygiene

- [ ] Only the requested change (AEP 7); no drive-by refactor or reformat.
- [ ] Dependencies justified (AEP 10).
- [ ] Documentation and `CHANGELOG.md` updated (AEP 11).
- [ ] Breaking changes answered in the template with migration notes.

## AEP 26 completion gate

Code builds · tests pass · Ruff passes · formatter passes · MyPy passes ·
documentation updated · acceptance criteria satisfied · no guardrail violated.
