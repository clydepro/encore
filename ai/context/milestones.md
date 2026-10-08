# Context Bundle — Milestones and Current State

What exists, what is scaffolding, and what comes next. Keep this page honest: it
is the first thing a session should read to avoid implementing something twice
or too early.

## Implementation order (AIG 21)

| # | Deliverable | State |
| - | ----------- | ----- |
| 1 | Repository initialization (PBK) | **Done — this is the bootstrap phase** |
| 2 | Core domain model | Not started |
| 3 | Event Bus | Not started |
| 4 | Configuration | Not started |
| 5 | Repositories | Not started |
| 6 | Library Builder | Not started |
| 7 | Search | Not started |
| 8 | Playback | Not started |
| 9 | Queue | Not started |
| 10 | Runtime database | Not started |
| 11 | FastAPI | Not started |
| 12 | HTMX | Not started |
| 13 | SSE | Not started |
| 14 | Administrative interface | Not started |
| 15 | Installer | Not started |
| 16 | Party Simulation | Not started |
| 17 | Documentation | Ongoing |

PBK's ten-milestone view groups the same work; both are listed in the README
roadmap.

## What exists today

- Fixed directory layout with empty, documented packages.
- `pyproject.toml` + `uv.lock` + `.python-version`; editable install.
- Ruff, MyPy (strict), pytest with category markers, coverage, pre-commit.
- Five GitHub Actions workflows; issue forms; PR template; labels; CODEOWNERS;
  Dependabot; branch protection definition.
- Documentation scaffolding and ADR-001…ADR-008.
- Test infrastructure: fixtures, temp SQLite helpers, mock mpv, Party Simulation
  profiles + loader, performance target registry, synthetic media hooks
  (placeholders).
- Architecture guardrail tests and `tools/check_links.py`.

## Explicitly not implemented

- Any HTTP endpoint, template, static asset or SSE stream.
- Any database schema, migration or query.
- Any mpv integration, queue rule, search implementation or service.
- The installer and `encore-install`.

Writing any of the above during bootstrap violates PBK 1 ("no application
functionality is implemented during this phase").

## Where to put the first feature

Milestone 2 starts in `encore/domain/` (entities) and `encore/events/` (event
types) with tests in `tests/unit/`. `apps/server/` and `apps/builder/` stay
empty until their wiring is real; `scripts/run-*.sh` already print the honest
placeholder message.

## Known open questions to resolve before v1

- Final licence choice among MIT/BSD-3/Apache-2.0 (SAPRS 15.15). Bootstrap uses
  MIT; changing it is a one-file, one-ADR decision.
- HTMX and Tailwind distribution in `encore/static/` (vendored vs CDN) — decided
  during milestone 12.
- Administrator credential mechanism (SAPRS 12.6) — decided during milestone 14.
