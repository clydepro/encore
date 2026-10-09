# Current Phase

**Phase 1 — Core Foundation: complete and merged** ([#20](https://github.com/clydepro/encore/pull/20),
commit `eda2d22` on `main`). **Phase 2 — persistence and the Library Builder (AIG
steps 5 and 6): not started; the decisions are settled and the code is not.**

Read this page first in a new session, then [`context/milestones.md`](context/milestones.md)
for what exists and what is scaffolding. Neither is authoritative: precedence is
task request → SAPRS → AIG → ADRs → AEP (AEP 2).

## What "Phase 1" was taken to mean

The phrase is not defined in the SAPRS, AIG or AEP. It was read as the AIG 21
steps that have no dependency on storage, playback or HTTP — **steps 2, 3 and
4** — which is PBK's milestone 2, "Core Framework" (configuration, dependency
injection, logging, Event Bus). Step 1, repository initialization, was already
complete.

If a different split was intended, the work below does not need to be redone: it
is the same code, and the parts of it that belonged to a different phase are
already separated by package.

## Delivered

| AIG 21 | Delivered as | Spec |
| ------ | ------------ | ---- |
| 2 — Core domain model | `encore/domain/` | SAPRS Ch. 4 |
| 3 — Event Bus | `encore/events/` | SAPRS 11.1–11.4, ADR-004 |
| 4 — Configuration | `encore/config/` | SAPRS Ch. 12 |
| (PBK 2 logging + DI) | `encore/services/` | AEP 15–16, AEP 9 |

`encore/utilities/clock.py` and `redaction.py` exist because both the domain and
the log formatter needed them and neither should own them.

## Verification state

- `scripts/check.sh` — all gates pass: ruff (lint + format), yamllint,
  markdownlint, secret scan, mypy strict, **342 tests**, 99% coverage of
  `encore/` against a 90% floor.
- `scripts/check.sh --slow` — 344 pass; the six skips are the performance and
  Party Simulation benchmarks, which name the milestone that will fill them in.
- `uv run python tools/check_links.py` — clean.
- The new guardrails were mutation-checked: a `source_ip` field on `QueueItem`,
  a domain→services import, a module-level `EventBus()`, an unfrozen event and a
  `write_text` in `encore/config/` each make CI fail. That is the only evidence
  that a guardrail is real.
- CI on the merged commit: 14 required checks green on `main`. One CodeQL alert
  (`py/clear-text-logging-sensitive-data`, tests/unit/test_logging_service.py:269)
  was dismissed as a false positive — that line is the redaction test, which logs
  a password unredacted on purpose. Under this repo's code-scanning merge
  protection an undismissed alert blocks the merge even with all 14 checks green.

## Explicitly not in this phase

Repositories and both database schemas; the Library Builder; search; playback;
the queue service; FastAPI, HTMX, SSE and the admin interface; the installer.
Nothing in `encore/repositories/`, `encore/playback/`, `encore/search/`,
`encore/api/`, `encore/controllers/`, `encore/templates/` or `encore/static/` was
written, and `apps/server/` and `apps/builder/` are still placeholders.

## Next

**AIG steps 5 and 6 together**: the two database schemas and their repositories
([issue #19](https://github.com/clydepro/encore/issues/19)), and the Library
Builder that fills one of them. They are inseparable in practice — the schema is
the Builder's output contract (ADR-010) — and both are now unblocked because the
two open design questions have been decided in writing:

- **ADR-009** settles the access layer, superseding ADR-003's "SQLAlchemy
  repositories for both" clause: raw `sqlite3` read-only for `library.db`,
  SQLAlchemy 2.x transactions for `runtime.db`, numbered forward-only migrations
  for the latter, version stamp inside the former, and a startup shape check on both
  stores. Its rationale was amended on the day of acceptance after measurement — the
  read-only guarantee comes from the connection URI, not the driver choice, and the
  performance argument for raw `sqlite3` is void at 15,000 songs. The decision
  stands on schema ownership. SQLAlchemy Core is recorded there as the rejected
  strongest alternative.
- **ADR-010** settles the Builder: stage-per-module pipeline, the Builder owns the
  DDL alone, a four-level metadata precedence rule with the path demoted to a hint,
  uncatatalogueable files reported rather than fatal, and `paths.music_dir` default
  `/opt/music`.

Both are Accepted and dated 2026-10-09. ADR-010's Context carries the measurements
the rules were written against, so read it before designing anything that touches
the corpus.

## Loose ends

- **Music corpus cleanup, before any Builder run.** Decided not to prune in order to
  simplify the Builder — SAPRS 6.8 forbids the Builder deleting user music and 6.4
  makes tags primary, so a Builder that only works on clean data is not the
  appliance. Two actions remain, and both are the operator's, not the code's:
  231 real-but-unplayable files (188 `.m4p` DRM, 41 `.wma`, 2 `.aif`) should move out
  of `/opt/music` or be transcoded so the build report's skip list stays readable;
  and 15 playable files have no usable artist (7 no artist and no title) that only a
  human can identify. The non-music strays (`.DS_Store`, GarageBand internals,
  `.mid`, `.m4v`) need nothing — `AudioFormat.for_path` ignores them.
- **`paths.music_dir` does not exist yet**, although SAPRS 12.2 requires a library
  location. It is a one-field change that must land with `examples/config.yaml` and
  the Administrator guide simultaneously (ADR-010 explains why two of three fails
  CI).
- **A regression test for the `slots` + `super()` trap** belongs in
  `tests/regression/` per AEP 13, but that suite's convention (and CI check) is
  one file per issue number and no issue exists for a defect found while writing
  code. The guard lives in `tests/unit/test_events.py`, named as a regression
  guard there; opening a `chore` issue and moving it is the tidy follow-up.
- **No issue was opened for AIG steps 2–4**, which CONTRIBUTING §2 asks for. The
  commit and the CHANGELOG entry are the record instead.
- **Human review is still a norm, not a gate.** GitHub will not let a sole
  maintainer approve their own pull request, so the reviewer checklist in the
  template is left unticked on purpose. Revisit alongside `required_approving_
  review_count` (currently 0) when a second developer arrives, with the two ADR
  questions above already closed.
