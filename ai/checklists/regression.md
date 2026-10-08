# Regression Checklist

Applied to every fixed defect (SAPRS 14.14, AEP 13). One missing line means the
fix is incomplete.

## Before touching the fix

- [ ] Reproduced from a clean checkout, with a recorded command.
- [ ] Root cause stated as a mechanism, not a symptom.
- [ ] Failing test written first and observed failing, with the failure output
      saved in the PR description.

## The regression test

- [ ] Lives in `tests/regression/test_issue_<number>_<slug>.py`.
- [ ] Docstring begins `Regression: #<number>` and names the mechanism.
- [ ] Exercises the real code path, not a re-implementation of the assertion.
- [ ] Fails on the pre-fix commit; passes on the fix commit. Prove both.
- [ ] No sleeps, no timing assumptions, no dependence on a developer machine.
- [ ] Deterministic inputs: temporary databases and `tests/support/` doubles.
- [ ] Named for the behaviour, e.g. `test_duplicate_song_appends_to_queue`.

## Coverage of the same class of bug

- [ ] Searched for the same mistake elsewhere (`rg` for the pattern) and either
      fixed or filed each hit.
- [ ] Added assertions at the layer that should have caught it (service unit
      test, guardrail test, schema validation).
- [ ] If the bug was possible because of an architecture violation, an ADR or a
      guardrail test now prevents the shape, not just the instance.

## Documentation

- [ ] `CHANGELOG.md` **Fixed** entry referencing the issue.
- [ ] `docs/Troubleshooting.md` updated if operators would have noticed it.
- [ ] Issue closed with a link to the test file.

## Suite health

- [ ] `scripts/test.sh` green.
- [ ] `scripts/check.sh --slow` green when the defect involved playback, queue,
      SSE or concurrency.
- [ ] Coverage did not fall.

## When the test is later deleted or weakened

- It needs a written justification in the PR and a link to the original issue,
  because the bug is still capable of returning.
