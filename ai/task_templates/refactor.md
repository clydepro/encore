# Task Template — Refactor

Refactor only when it improves clarity, reduces duplication or simplifies
maintenance — never cosmetically (AEP 24).

```text
Task:
<One sentence: the structural change.>

What is hard today:
<Named files and the specific pain: duplicated rule, two responsibilities in one
 service, untestable coupling.>

Proposed shape:
<Ownership, interfaces, dependency direction afterwards.>

Behaviour must not change:
<Confirm: no observable change, no API change, no data change.>

Relevant documents:
- SAPRS: <the section the new shape must satisfy>
- ADRs: <boundary changes require an ADR — list or state "none needed">

Equivalence evidence:
<Which existing suites cover it; which tests to write first, before moving code.>

Migration steps:
<Ordered, each individually mergeable and green.>

Out of scope:
<Renaming neighbours, drive-by modernisation, formatting untouched files.>

Definition of done:
- Existing tests unchanged and passing (or the change in expectations justified)
- scripts/check.sh green
- No new dependency; no guardrail newly violated
- Docs/handbook updated where structure is described
```

## Rules

- Write or strengthen tests **before** restructuring, then refactor to green.
- A refactor that changes behaviour is two changes: the refactor, then the
  behaviour change.
- Schema or event-payload changes are not refactors: they need migrations,
  tests, documentation and backward-compatibility analysis (AEP 18), and an ADR.
