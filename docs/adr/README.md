# Architectural Decision Records

Institutional memory for Encore (SAPRS 15.8, AEP 2).

Every significant architectural decision is recorded here as a short, immutable
document. Code explains *how*; an ADR explains *why* — and, just as importantly,
what was rejected.

## Index

| ID      | Title                                      | Status    |
| ------- | ------------------------------------------ | --------- |
| ADR-001 | Separate Library Builder from Server       | Accepted  |
| ADR-002 | HTMX Instead of SPA                        | Accepted  |
| ADR-003 | SQLite as the Storage Engine               | Accepted  |
| ADR-004 | Internal Event Bus                         | Accepted  |
| ADR-005 | mpv Playback Engine                        | Accepted  |
| ADR-006 | Immutable Library Database                 | Accepted  |
| ADR-007 | Anonymous Guest Model                      | Accepted  |
| ADR-008 | Appliance-First Philosophy                 | Accepted  |

## Format

Each ADR contains, in this order:

1. **Title** — one line, `# ADR-NNN: <Title>`
2. **Status** — Proposed | Accepted | Superseded by ADR-NNN
3. **Date** — ISO 8601 date of acceptance
4. **Context** — the force at play, referencing the SAPRS
5. **Decision** — the change, stated actively
6. **Consequences** — trade-offs, both directions
7. **Alternatives considered** — what was rejected and why

## Working with ADRs

- ADRs are numbered sequentially and never reused.
- Changing a decision means writing a new ADR that supersedes the old one; the
  superseded record is kept and marked as such.
- Implementation milestones in AIG Chapter 21 may not contradict an Accepted
  ADR without a superseding ADR first.
- The repository layout itself is fixed by PBK Chapter 2 and may only change by
  ADR.
- The mandatory guardrails in SAPRS 11.10 are enforced in code by
  `tests/unit/test_architecture_guardrails.py`.

## Writing a new ADR

Copy the template below, add a row to the index, and open a pull request using
the *Refactoring Proposal* or *Feature Request* issue template as context.

```markdown
# ADR-NNN: <Title>

## Status

Accepted

## Date

YYYY-MM-DD

## Context

...

## Decision

...

## Consequences

...

## Alternatives considered

...
```
