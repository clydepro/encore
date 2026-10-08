# Code Review Prompt

Run this as a **separate pass** from implementation, ideally in a fresh session
with no memory of writing the code (AEP Appendix C). Do not let the author model
review its own output.

```text
Review this implementation as a senior Encore maintainer.

Do not rewrite the code.

Instead:

1. Identify architectural violations.
2. Identify Clean Architecture violations.
3. Identify Event Bus misuse.
4. Identify testing gaps.
5. Identify documentation gaps.
6. Identify performance concerns.
7. Identify security concerns.
8. Verify compliance with the AI Engineering Playbook.
9. Recommend improvements in priority order.

Do not invent requirements outside the SAPRS.
```

## Attach to the review

- The diff, the issue text and the PR template answers.
- The SAPRS chapters named in the PR.
- The relevant ADRs.
- `ai/context/architecture.md` and, if the change touches boundaries,
  `ai/context/guardrails.md`.

## Output format

Write findings to `ai/reviews/`, one file per review, using
[`../reviews/report-template.md`](../reviews/report-template.md). Rules:

- Every finding cites the rule it violates (chapter/section), or is labelled
  "judgement".
- Priority order: blocking architectural violation → missing test →
  missing documentation → clarity → performance.
- No style opinions: Ruff owns style.
- Do not propose refactors unrelated to the change (AEP 7, AEP 24).

## Blocking questions

Answer each with a citation or block the merge:

1. Does any new coupling bypass the Event Bus?
2. Does a controller or template now contain a business rule?
3. Does anything write to `library.db` at runtime?
4. Does the Builder import playback, or the Server import the Builder?
5. Is there a test that would have failed before this change?
6. If a defect was fixed, is there a `tests/regression/` entry?
7. Is guest anonymity intact, and are secrets out of logs and config?
8. Which SAPRS/ADR text authorises the behaviour a reviewer would otherwise
   have to guess at?
