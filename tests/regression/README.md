# tests/regression

Every bug fixed becomes a permanent regression test (SAPRS 14.14, AEP 13).

Rules:

- Name the file `test_issue_<issue>_<slug>.py`.
- Start the module docstring with `Regression: #<issue>` — CI checks the link.
- Demonstrate the original failure first, then the corrected behaviour.
- Never delete a regression test because it is "annoying"; fix the cause.

A pull request that fixes a bug without adding a test here is incomplete.
