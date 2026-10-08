# Task Template — Bug Fix

A bug fix is a change plus a permanent guard. Half the work is proving the
failure was real (SAPRS 14.14, AEP 13).

```text
Task:
<One sentence: the behaviour that must stop happening.>

Symptom:
<What a user sees. Include the log line, HTTP status or audio artefact.>

Reproduction:
1. <steps, from a clean state, deterministic>
2. …
Fails since: <version/tag/commit, or "unknown">

Relevant documents:
- SAPRS: <the requirement being violated, by chapter and section>
- ADRs: <if the fix touches an accepted decision>

Root cause:
<The mechanism, not the symptom. "The queue is advanced twice" — not
 "the queue misbehaves".>

Fix approach:
<Where the correction lands and why that layer owns it.>

Out of scope:
<Cleanup that is tempting but forbidden.>

Required tests:
- Regression: tests/regression/test_issue_<number>_<slug>.py — must fail before
  the fix and pass after it
- Unit/Integration: <as appropriate>

Definition of done:
- Regression test demonstrated red on the pre-fix commit and green after
- scripts/check.sh green
- CHANGELOG.md entry under Fixed
- docs/Troubleshooting.md updated if an operator would notice
- Issue closed with the test linked
```

## Rules

- Write the failing test first. If it cannot fail, you have not reproduced the
  bug — stop and reproduce it.
- Do not fix adjacent problems in the same change (AEP 7).
- If the root cause is an architectural violation, say so in the PR and open a
  refactoring proposal rather than patching around it.
