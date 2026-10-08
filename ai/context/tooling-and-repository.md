# Context Bundle — Tooling, CI and Repository Rules

For sessions working on the repository itself: tooling, workflows, templates,
scripts, documentation or test infrastructure (PBK scope).

## Project configuration

- One source of truth: `pyproject.toml` (project metadata, dependencies, Ruff,
  MyPy, pytest, coverage, detect-secrets).
- `uv` manages Python and dependencies; `.python-version` pins the interpreter;
  `uv.lock` is committed and CI installs with `--frozen`.
- The project installs editable; `import encore` must work after
  `scripts/bootstrap.sh`.
- Dev tooling lives in the `dev` dependency group: ruff, mypy, pytest,
  pytest-asyncio, pytest-cov, pre-commit, yamllint, types-PyYAML,
  detect-secrets.
- MyPy strictness, Ruff rule selection and pytest markers live in
  `pyproject.toml`; hook order lives in `.pre-commit-config.yaml`.

## Commands

```bash
scripts/bootstrap.sh   # deps + hooks
scripts/lint.sh        # ruff, yamllint, mypy
scripts/format.sh      # ruff --fix, ruff format, markdownlint --fix
scripts/test.sh        # unit + integration
scripts/test.sh --all  # + performance, party simulation
scripts/check.sh       # the whole gate; --slow adds long suites
scripts/run-server.sh  # placeholder until milestone 11
scripts/run-builder.sh # placeholder until milestone 6
```

## Style decisions already made

- Ruff is both linter and formatter — Black is intentionally absent (PBK 4
  allows this; documented in `docs/Developer/Continuous-Integration.md`).
- Line length 100. Rule families include bugbear, isort, annotations, naming,
  pathlib, return/argument checks, bandit-style security checks, simplify and pyupgrade.
- MyPy runs in strict mode over `encore`, `tests` and `tools`.
- Markers: `unit`, `integration`, `regression`, `performance`,
  `party_simulation`, `e2e`, `slow`, `chaos`; category markers are applied by
  directory in `tests/conftest.py`.
- Coverage floor starts at 0 during bootstrap and rises with milestones toward
  SAPRS 14.16 (≥ 90% overall).

## CI (five workflows, PBK 6)

`validate.yml`, `test.yml`, `docs.yml`, `security.yml`, `release.yml`. Job names
are referenced by `.github/branch_protection.json`, so renaming a job means
re-running `scripts/configure-branch-protection.sh`.

## Repository layout is fixed (PBK 2)

Adding, removing or renaming top-level or `encore/` sub-packages requires an
ADR. `.github/`, `ai/`, `apps/`, `docs/`, `scripts/`, `tests/`, `tools/`,
`assets/`, `examples/` and `encore/` are the sanctioned homes; put new files in
one of them rather than inventing a directory.

## GitHub artifacts

- Issue forms: bug, feature, documentation, performance, security concern,
  refactoring proposal, question (PBK 8) — collect reproduction or evaluation
  data, not narrative.
- PR template carries the PBK 9 fields plus the guardrail checklist.
- Labels come from `.github/labels.yml` (PBK 10), applied by
  `scripts/sync-labels.sh`; area labels use an `area:` prefix to avoid the
  `documentation` name collision.
- Dependabot: weekly, grouped minors/patches, actions and pip.

## Naming and commits (SAPRS 15.6, 15.12)

PascalCase classes, snake_case functions/modules, UPPER_SNAKE constants;
`feat|fix|docs|test|refactor|chore|perf(scope): imperative subject`.

## Definition of done for bootstrap-style work

See `ai/checklists/definition-of-done.md`; the phase acceptance list lives in
PBK Chapter 19 and is partly asserted by
`tests/unit/test_test_harness.py`.
