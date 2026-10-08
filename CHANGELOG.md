# Changelog

All notable changes to Encore are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html), and versions are
tagged `vX.Y.Z` (PBK Chapter 17).

This file is updated by every pull request that changes behaviour, interfaces or
the build and operations tooling, under **Unreleased**. The release process moves
that section into a dated version heading.

## Unreleased

### Added

- Repository bootstrap (PBK): the fixed directory layout, `pyproject.toml` as
  the single project configuration, `uv`-managed dependencies with a lockfile,
  and an editable development install.
- Quality tooling: Ruff (lint + format), MyPy in strict mode, pytest with
  category markers, Coverage.py and pre-commit hooks covering formatting,
  linting, import order, whitespace, EOF, YAML, TOML, Markdown and secret
  detection.
- CI/CD: `validate.yml`, `test.yml`, `docs.yml`, `security.yml` (CodeQL, OSV
  audit, dependency review, secret scan) and `release.yml` for tagged releases.
- GitHub configuration: CODEOWNERS, Dependabot, branch protection definition,
  the label taxonomy from PBK Chapter 10, seven issue forms and a pull request
  template.
- Documentation scaffolding: README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY,
  CHANGELOG, LICENSE, the documentation map, and outlines for the user guide,
  administrator guide, developer handbook, API reference, troubleshooting guide
  and release process.
- Initial ADRs 001–008, covering builder/server separation, HTMX over a SPA,
  SQLite storage, the internal Event Bus, mpv playback, the immutable library
  database, the anonymous guest model and the appliance-first philosophy.
- Test infrastructure: shared fixtures, temporary SQLite helpers, a mock mpv
  JSON-IPC double, Party Simulation load profiles with a loader, performance
  target registry, and synthetic media generator hooks (placeholders).
- Architecture guardrail tests that enforce the SAPRS 11.10 rules in CI.
- AI development support materials in `ai/`: the standard prompt header, task
  templates, code-review prompt, feature and regression checklists and context
  bundles derived from the AIG.
- Developer scripts: `bootstrap.sh`, `lint.sh`, `format.sh`, `test.sh`,
  `check.sh`, `run-server.sh`, `run-builder.sh`, `sync-labels.sh`,
  `configure-branch-protection.sh`, `setup-github-repo.sh`.

### Changed

- Not yet applicable: no application behaviour exists to change.

### Deprecated

- None.

### Removed

- None.

### Fixed

- `scripts/sync-labels.sh` and `scripts/setup-github-repo.sh` failed with
  "unknown flag: --repo" on the GitHub CLI shipped by Debian (2.23), where
  `gh api` has no such flag; repository selection now travels in the API path
  and `gh label`'s own `--repo` is used where it is supported.
- Branch protection required `CodeQL` and `Dependabot`, checks that no workflow
  produces, so every pull request would have waited on them forever. The
  required contexts are the real `Workflow / job` names, and
  `tests/unit/test_ci_contract.py` keeps them in sync with the workflows.
- `Security / vulnerabilities` failed on every run because of an unsupported
  `uv audit --strict` flag.
- `Security / codeql` failed because GitHub rejects SARIF from this workflow
  while the code-scanning **default setup** is enabled. The setup script now
  turns the default setup off, and the workflow analyses `python` and `actions`
  so nothing is lost by doing so.
- CI actions were referenced by moving major tags that some authors never
  publish, which failed jobs at "Set up job"; every action is now pinned to a
  full version tag and a test rejects moving references.
- Branch protection required approvals that nobody could give. A single
  maintainer cannot approve their own pull request, so "one approving review",
  "code owner review" and "approval of the most recent push" made every pull
  request unmergeable — an admin merge does not bypass review requirements. The
  review gates are off until a second person with write access joins (see
  `docs/Developer/Repository-Administration.md`); all fourteen status checks
  still gate every merge, for admins included.

- `scripts/setup-github-repo.sh --dry-run` failed immediately: it passed
  `--repo` to `tools/sync_labels.py`, which takes the repository positionally.
  `scripts/sync-labels.sh` also dropped its arguments, so `--prune` and
  `--dry-run` did nothing.
- Two setup steps were listed as "UI only" when they do have endpoints: private
  vulnerability reporting (`PUT …/private-vulnerability-reporting`) and the
  merge/branch options (`PATCH …/`). The script now applies six steps and the
  remaining checklist is genuinely short.

### Security

- Secret scanning, dependency vulnerability auditing and CodeQL analysis wired
  into CI; reporting process documented in `SECURITY.md`.

## 0.0.0 - 2026-10-07

### Added

- Planning documents committed: SAPRS, AI Implementation Guide, AI Engineering
  Playbook and Project Bootstrap Kit.
- Repository initialised.
