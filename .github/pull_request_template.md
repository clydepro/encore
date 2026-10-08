# Summary

<!-- One paragraph: what does this change do? Assume the reviewer has not read
     the issue thread. -->

## Related issue

Closes #

## Motivation

<!-- Why is this needed now? Link the SAPRS requirement or the observed defect.
     "It felt better" is not a motivation (AEP 3). -->

## Design notes

<!-- How it works, and why it works this way. Mention the owning service and the
     interfaces touched. Delete if the change is a one-liner. -->

## Scope

- [ ] This change is limited to the task it claims (AEP 7)
- [ ] No unrelated refactors, renames or reformatting
- [ ] No new dependency, or the dependency is justified below

**Dependency justification** (delete if none):

<!-- Standard library insufficient because … Existing dependency cannot because …
     Maintenance status … Size/impact … (AEP 10) -->

## Testing performed

<!-- What you ran, and what you added. -->

- [ ] `scripts/check.sh` passes locally (ruff, format, mypy, pytest, coverage)
- [ ] Unit tests added/updated: `tests/unit/`
- [ ] Integration tests added/updated: `tests/integration/`
- [ ] Regression test added for a fixed defect: `tests/regression/` (SAPRS 14.14)
- [ ] End-to-end / UI tests where behaviour changed: `tests/`
- [ ] Party Simulation run where runtime behaviour changed (SAPRS 14.11)

Commands run and results:

```bash
scripts/check.sh
```

## Documentation updated

- [ ] API reference (`docs/api/`) where endpoints or schemas changed
- [ ] User / Administrator guide where behaviour visible to users changed
- [ ] Developer handbook where workflow, configuration or testing changed
- [ ] `CHANGELOG.md` entry under **Unreleased**
- [ ] Docstrings for new public classes/functions (SAPRS 15.7)

## SAPRS chapters affected

<!-- e.g. "Chapter 7 (Playback Service)". "None" if purely internal. -->

## ADR required?

- [ ] No
- [ ] Yes — ADR added: `docs/adr/ADR-NNN-....md` (supersedes ADR-NNN if relevant)

<!-- An ADR is required for: architecture changes, new dependency with broad
     impact, storage schema changes, public API semantics, security model
     changes (SAPRS 15.8, AEP 18). -->

## Breaking change?

- [ ] No
- [ ] Yes — migration/guidance: <!-- what must an operator or API consumer do? -->

## Architectural guardrails

- [ ] Event Bus still mediates cross-service notification (no new direct coupling)
- [ ] No business logic in controllers or repositories
- [ ] `library.db` still never written at runtime
- [ ] Builder still does not import playback
- [ ] Templates still contain presentation only
- [ ] Guest anonymity preserved (ADR-007)

## AI assistance disclosure (AEP 27, SAPRS 15.14)

- [ ] No AI assistance was used
- [ ] AI assistance used; the human author(s) above reviewed every line, and the
      session started from `ai/prompts/standard-prompt-header.md`

## Reviewer checklist (leave for the reviewer)

- [ ] Architecture respected (SAPRS 11.10)
- [ ] Tests demonstrate the change, including the failure it prevents
- [ ] Type hints complete, logging structured, errors actionable (AEP 15/16)
- [ ] Performance acceptable against SAPRS 1.8 targets
- [ ] AEP 25/26 checklists satisfied
