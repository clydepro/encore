# Task Template — Feature

Copy into the issue or the model's first prompt. Everything in angle brackets is
required; delete the sections that do not apply rather than leaving blanks
(AEP Appendix B).

```text
Task:
<One sentence: what will exist after this that did not exist before.>

Relevant documents:
- SAPRS: <chapters, e.g. Chapter 8 (Queue & Playback Orchestration)>
- AIG: <sections, e.g. 7, 9, 12, 21 step 9>
- ADRs: <004, 006, or "none">

Scope:
- <files/services created or changed>
- <endpoints or events added>
- <database tables affected, if any>

Out of scope:
- <what must not change, and what tempts you but is forbidden>

Acceptance criteria:
- <observable behaviour, phrased so a reviewer can test it>
- <performance target from SAPRS 1.8 where relevant>

Required tests:
- Unit: <tests/unit/...>
- Integration: <tests/integration/...>
- Regression: <n/a, or the issue number>

Definition of done:
- Code implemented, tests passing
- scripts/check.sh green (ruff, format, yamllint, mypy, pytest, links)
- Documentation updated (name the files)
- CHANGELOG.md entry under Unreleased
- No guardrail violated, and no new dependency (or a justification)
```

## Fill-in notes

- **Name the owning service** before listing files (AEP 6): "the Queue Service
  owns this rule" is the sentence that prevents most bad designs.
- If the feature needs a new event, add the event name and payload here — the
  vocabulary is reviewed, not improvised (ADR-004).
- If a schema change is required, say so: migrations, tests, docs and
  backward-compatibility analysis come with it (AEP 18).
- If you cannot state the acceptance criteria, the task is not ready; open a
  Question issue instead.

## Checklist

See [`../checklists/feature-implementation.md`](../checklists/feature-implementation.md).
