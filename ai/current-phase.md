# Current Phase

**Phase 1 — Core Foundation. Complete, pending commit.**

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

## Explicitly not in this phase

Repositories and both database schemas; the Library Builder; search; playback;
the queue service; FastAPI, HTMX, SSE and the admin interface; the installer.
Nothing in `encore/repositories/`, `encore/playback/`, `encore/search/`,
`encore/api/`, `encore/controllers/`, `encore/templates/` or `encore/static/` was
written, and `apps/server/` and `apps/builder/` are still placeholders.

## Next

**[Issue #19 — Persistence layer: `library.db` and `runtime.db` schemas and
repositories (AIG step 5)](https://github.com/clydepro/encore/issues/19).** It is
the dependency for both the Library Builder (step 6) and everything that reads a
song, so it goes first.

Two things in that issue need a decision before code, not during it: whether
SQLAlchemy earns its place in `runtime.db`, and how `runtime.db` is versioned
(SAPRS 5.7 permits rebuild-or-migrate and does not choose).

## Loose ends

- **Uncommitted work.** Everything listed above is in the working tree, not on a
  branch. `CONTRIBUTING.md` §7 wants issue → feature branch → PR, and branch
  protection makes `main` non-pushable with linear history required. The bootstrap
  commit landed on `main` directly (it predates the protection it defines); this
  phase is best shipped as a branch and a pull request, with #19 named as the
  follow-on rather than the parent, since no issue was opened for steps 2–4.
- **A regression test for the `slots` + `super()` trap** belongs in
  `tests/regression/` per AEP 13, but that suite's convention (and CI check) is
  one file per issue number and no issue exists for a defect found while writing
  code. The guard lives in `tests/unit/test_events.py`, named as a regression
  guard there; opening a `chore` issue and moving it is the tidy follow-up.
- **`PHASE-1` was never a label or a milestone**, so nothing in GitHub marks this
  work as one unit. The commit and the CHANGELOG entry are the record.
