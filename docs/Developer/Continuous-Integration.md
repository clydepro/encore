# Continuous Integration

PBK Chapter 6 asks for five workflows; this is what each one guarantees and what
to do when it disagrees with you.

| Workflow | File | Triggers | Required checks |
| -------- | ---- | -------- | --------------- |
| Validate | [`validate.yml`](../../.github/workflows/validate.yml) | PR, push to `main` | `Validate / dependencies`, `Validate / lint`, `Validate / types`, `Validate / unit (3.12)`, `Validate / unit (3.13)` |
| Test | [`test.yml`](../../.github/workflows/test.yml) | PR, push to `main`, nightly | `Test / integration`, `Test / arm64 smoke` |
| Documentation | [`docs.yml`](../../.github/workflows/docs.yml) | PR, push to `main` | `Docs / markdown`, `Docs / links`, `Docs / structure` |
| Security | [`security.yml`](../../.github/workflows/security.yml) | PR, push, weekly | `Security / vulnerabilities`, `Security / secrets`, CodeQL |
| Release | [`release.yml`](../../.github/workflows/release.yml) | `v*` tags | not required for merge |

## Validate — the fast gate

1. `uv lock --check` — the lockfile must match `pyproject.toml`.
2. `uv sync --frozen --all-groups` — reproducible install, editable project.
3. Ruff lint + Ruff format verification + yamllint.
4. MyPy in strict mode.
5. `pytest tests/unit` on Python 3.12 and 3.13, including the architecture
   guardrail tests.

Formatting is owned by **Ruff format**, not Black. PBK 4 allows either "if
adopted consistently"; one tool means one opinion, no fight over blank lines,
and pre-commit/CI cannot disagree. If that is ever revisited it needs an ADR.

## Test

Integration tests plus a coverage report uploaded as an artifact
(`coverage.xml`), and an arm64 smoke job because the target device is a
Raspberry Pi 4. Nightly runs the slow suites: performance and the baseline party.

## Documentation

- `markdownlint-cli2` with `.markdownlint-cli2.jsonc`. The vendored planning
  documents (SAPRS/AIG/AEP/PBK) are excluded so a formatter never rewrites an
  upstream spec.
- `tools/check_links.py` verifies internal links and anchors (generated docs are
  excluded, and there are none yet).
- `tests/unit/test_test_harness.py` asserts the required documents, ADRs and
  `.github` files still exist.

## Security

- CodeQL (python, `build-mode: none`).
- `uv audit` against OSV for the locked dependency set.
- `dependency-review-action` on PRs, rejecting high-severity advisories and
  copyleft licenses (SAPRS 15.10).
- `detect-secrets` against `.secrets.baseline`.
- GitHub-native secret scanning and push protection are repository settings, not
  workflow steps: see [Repository administration](Repository-Administration.md).

## Release

Builds and verifies, then publishes a GitHub release and (for non-`rc` tags, via
the `pypi` environment) the distributions. See
[Release process](Release-Process.md).

## Caching and cost

`astral-sh/setup-uv` caches downloads; concurrency groups cancel superseded runs
on PRs. Nothing in the per-commit path runs a long suite — that is deliberate
(AIG 22: do not optimize before measuring, and do not make CI slow on purpose).

## Adding a workflow

Open an issue first. PBK 6 fixes the set at five; more workflows need a stated
gap, and every gate added to `main`'s required checks is a tax on every future
contribution.
