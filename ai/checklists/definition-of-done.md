# Definition of Done (AIG 20, AEP 26)

A task is complete only when every line below is true. Copy this list into the
PR description and tick it honestly.

- [ ] Code implemented as scoped — nothing more.
- [ ] Tests pass, and new behaviour is covered by a test that would have failed
      before the change.
- [ ] Type checking passes: `uv run mypy`.
- [ ] Linting and formatting pass: `uv run ruff check .` and
      `uv run ruff format --check .`.
- [ ] `scripts/check.sh` is green end to end.
- [ ] Documentation updated (guide, API reference, ADR, docstrings, changelog).
- [ ] Acceptance criteria from the issue satisfied and demonstrable.
- [ ] No architectural guardrail violated, and no new justification needed in
      the PR body.

## Bootstrap-phase additions

Because there is no application code yet, "done" for PBK work also means:

- [ ] Layout still matches PBK Chapter 2 (checked by
      `tests/unit/test_architecture_guardrails.py`).
- [ ] New tooling is configured in `pyproject.toml`, not in ad-hoc scripts.
- [ ] CI changes keep the five workflows green on an empty project.
- [ ] Any new required status check is added to
      `.github/branch_protection.json` in the same PR.

## Not done, even if the code works

- Feature flag with no removal plan.
- Test skipped "for now" without an issue number in the reason.
- Documentation promised in the PR body instead of the diff.
- A dependency added because it was familiar.
- A bug fixed without a regression test.
