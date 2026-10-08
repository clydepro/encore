# Encore Developer Handbook

The working guide for people changing Encore. Start with
[Getting-Started.md](Getting-Started.md) if your laptop does not have the repo
yet.

- [Getting started](Getting-Started.md) — clone, install, first check run.
- [Testing](Testing.md) — suites, fixtures, doubles, Party Simulation.
- [Continuous integration](Continuous-Integration.md) — the five workflows and
  what each gate means.
- [Repository administration](Repository-Administration.md) — branch protection,
  labels, Dependabot, CodeQL.
- [Release process](Release-Process.md) — versioning and the release checklist.
- [Documentation map](../README.md) — which document is authoritative.

## Status

Steps 1–4 of [AIG Chapter 21](../AIG/AIImplementationGuide_AIG.md) are
implemented: the domain model, the Event Bus and configuration, with structured
logging and a composition root. There is still no HTTP interface, no database and
no player — those arrive at steps 11, 5/10 and 8. "How do I implement X" is
answered by the SAPRS and the ADRs for what is not built yet, and by the code in
`encore/` for what is.

## The shape of a change

```text
Issue → Feature branch → Design → Implement → Test → Review → Merge
```

(SAPRS 15.11.) `main` is always deployable. Incomplete work stays on a branch.

## What belongs where

| Directory | Owns | Must not |
| --------- | ---- | -------- |
| `encore/domain/` | Entities, value objects, rules | Import frameworks |
| `encore/services/` | One capability per service | Reach into another service's internals |
| `encore/events/` | Immutable events, the Event Bus | Carry HTTP concepts |
| `encore/repositories/` | SQL, sessions, row mapping | Contain business rules |
| `encore/controllers/` | Request → service call | Touch SQLite |
| `encore/api/` | FastAPI wiring, SSE, fragments | Contain policy |
| `encore/playback/` | mpv process and IPC | Know that a browser exists |
| `encore/search/` | FTS5 queries | Mutate anything |
| `encore/templates/` | Presentation only | Make decisions |
| `apps/builder/` | Offline library construction | Import playback |
| `apps/server/` | The running appliance | Write to `library.db` |

The rules in that table are checked by
`tests/unit/test_architecture_guardrails.py`, not by goodwill.

## Naming and style

- PascalCase classes, snake_case functions and modules, UPPER_SNAKE constants
  (SAPRS 15.6).
- Python 3.12+ syntax, type hints everywhere, `pathlib` for paths, dataclasses
  or Pydantic models for data, enums for fixed sets (SAPRS 15.5).
- Constructor dependency injection; no module-level singletons, no global state.
- Docstrings say *why*; comments are rare and explain reasoning (SAPRS 15.7).
- Ruff owns linting and formatting (the formatter replaced Black deliberately —
  see [Continuous integration](Continuous-Integration.md)).

## Reading list for a first week

1. SAPRS Chapter 1 (what the thing is) and Chapter 2 (how it is arranged).
2. ADR-001, ADR-004, ADR-006, ADR-008 (the four decisions that shape everything).
3. SAPRS Chapter 11 (service composition and guardrails).
4. AIG Chapters 4, 7, 8, 21 (rules, services, events, order of work).
5. AEP Chapters 4–13 (how a change is made here).

## Glossary

- **Appliance** — the installed, running box; not the repository.
- **Builder** — `apps/builder`, the offline library producer (ADR-001).
- **Server** — `apps/server`, the always-on runtime.
- **Library** — 15,000 songs plus metadata, materialised as `library.db`.
- **Runtime state** — queue, history, statistics; `runtime.db` only.
- **Event** — an immutable fact already published on the Event Bus.
- **Guest** — an anonymous LAN user (ADR-007).
- **Administrator** — an authenticated operator (SAPRS 1.3).
