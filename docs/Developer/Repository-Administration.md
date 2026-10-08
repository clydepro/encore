# Repository Administration

One-time and periodic maintainer tasks (PBK Chapter 7, Chapter 10). All of them
are repository *settings*, which cannot live in the Git tree — so they are
written down and scripted instead.

Requires the GitHub CLI with admin access: `gh auth login`.

## First-time setup

Push first — protection rules and workflows only take effect against commits
that exist on GitHub:

```bash
git push -u origin main                      # protection and CI apply to what is on GitHub
scripts/setup-github-repo.sh                 # steps 1-6 below, idempotent
scripts/setup-github-repo.sh --verify        # read-only status of all of it
```

What the script applies:

1. **Labels** — the `.github/labels.yml` taxonomy via `tools/sync_labels.py`.
   GitHub's own default labels are reported as undeclared and never deleted.
2. **Branch protection for `main`** — from
   [`.github/branch_protection.json`](../../.github/branch_protection.json);
   see [Required checks](#required-checks) and [Reviews](#reviews).
3. **Dependabot alerts** — which also switches on the dependency graph; version
   updates come from the committed `.github/dependabot.yml`.
4. **Code scanning** — the CodeQL *default setup* is switched off, because
   GitHub refuses SARIF from our `security.yml` while it is enabled.
5. **Private vulnerability reporting** — `SECURITY.md` points at it.
6. **Merge and branch options** — rebase and squash only (matching
   `required_linear_history`), and delete head branches after merging.

What genuinely remains in the web UI, for this plan:

- **Settings → Security → Code security and analysis → Secret scanning and push
  protection**. Alerts are already collected for public repositories, but the
  push-protection toggle has no endpoint here.
- **Settings → Environments → `pypi`** with required reviewers, if and when this
  project publishes packages (`release.yml` expects that name).
- **Settings → General → Pull Requests → merge queue** — optional; see PBK 7.
- Signed commits — an optional extra rule for release branches; ask maintainers
  to configure SSH or GPG signing.

## Required checks

The `contexts` list in `.github/branch_protection.json` names workflow jobs
exactly as they appear in the Checks tab. After changing a job name, re-run
`scripts/configure-branch-protection.sh`, otherwise PRs wait forever on a check
that will never come.

Two traps worth remembering:

- A context that no workflow ever reports keeps every PR at "Expected" forever.
  That includes plausible-looking names: the CodeQL checks are
  `Security / codeql (python)` and `Security / codeql (actions)` — not `CodeQL` —
  and there is no `Dependabot` check at all. A matrix job's check name is its
  `name:` with the matrix values substituted, so renaming or re-matrixing a job
  changes what branch protection must list.
  `tests/unit/test_ci_contract.py` expands the names itself and fails if the two
  files disagree; `scripts/setup-github-repo.sh --verify` prints the live list.
- A job guarded by `if:` and reported as *skipped* satisfies a required check;
  one that never runs does not. `Security / dependency review` therefore works
  as a required check even though it only runs on pull requests.
- A *skipped matrix job* reports under its unexpanded name — GitHub has no matrix
  value to substitute. That is why a pull request whose `dependencies` job fails
  shows `Validate / unit (${{ matrix.python-version }})` instead of the two
  required contexts: the PR is already red, but the Checks tab looks odd.

## Reviews

`main` is protected for everyone, including admins (`enforce_admins`), so a merge
is only possible when the fourteen required checks are green — a bypass is not an
option here, and `gh pr merge --admin` will not help.

Review requirements are deliberately **off** while the project has one maintainer.
GitHub will not let an author approve their own pull request, so
`required_approving_review_count: 1` (or `require_last_push_approval: true`) makes
every pull request unmergeable: the merge fails with "At least 1 approving review
is required by reviewers with write access" and there is nobody to satisfy it.

When a second person with write access joins, turn the gates back on in
`.github/branch_protection.json` and re-run
`scripts/configure-branch-protection.sh`:

```json
"required_approving_review_count": 1,
"require_code_owner_reviews": true,
"require_last_push_approval": true
```

`CODEOWNERS` is already maintained so that change is a one-line flip, and
`tests/unit/test_ci_contract.py` documents the current settings inline.

## Labels

`.github/labels.yml` is the source of truth for the PBK 10 taxonomy: types
(`bug`, `feature`, `enhancement`, `documentation`, `refactor`, `test`, `chore`),
priorities (`P0`–`P3`), areas (`area:playback` … `area:documentation`) and
statuses (`needs-triage`, `in-progress`, `blocked`, `ready-for-review`,
`good-first-issue`, `help-wanted`).

Area labels carry a prefix because `documentation` appears twice in the PBK
table. Changing a name means updating this file, `labels.yml` and every issue
template that references it in one PR.

```bash
scripts/setup-github-repo.sh                 # applies the taxonomy
scripts/sync-labels.sh --prune --dry-run     # review drift only (tools/sync_labels.py)
```

## Dependencies

Two mechanisms, deliberately separated:

- **Vulnerabilities** are handled by Dependabot *security* updates (on as soon as
  Dependabot alerts are enabled) plus `uv audit` against OSV in
  `Security / vulnerabilities`. Those PRs touch `uv.lock`, so they resolve and
  they pass.
- **Freshness** is handled by Dependabot *version* updates with
  `versioning-strategy: lockfile-only`: one grouped PR per Tuesday run, changing
  `uv.lock` and nothing else. `pyproject.toml` stays the declaration of minimum
  compatible versions (SAPRS 15.10) rather than a record of what was latest.

The default strategy was tried first and did not work here: Dependabot raised the
`>=` floors in `pyproject.toml`, never re-ran `uv lock`, and produced eight PRs on
which every job failed with "the lockfile is out of date". If you see one of those
again, the ecosystem has drifted back from `lockfile-only`.

Manual refresh, when a dependency is needed for real (quarterly at most):

```bash
uv lock --upgrade && scripts/check.sh        # resolve, then prove it
uv lock --upgrade-package pillow             # one package at a time, preferred
```

## Automation inventory

| Automation | Where | Cadence |
| ---------- | ----- | ------- |
| Pre-commit hooks | `.pre-commit-config.yaml` | every local commit |
| Validate / Test / Docs / Security workflows | `.github/workflows/` | PR + push (nightly for slow suites) |
| Dependabot | `.github/dependabot.yml` | weekly, Tuesday |
| CodeQL | `security.yml` | PR, push, Monday |
| Issue triage | labels applied by templates | on issue creation |

## Onboarding a contributor

1. Invite as a collaborator with **Read**; triage after their first merged PR.
2. Add them to `.github/CODEOWNERS` for the area they own (one capability, one
   owner — SAPRS 11.6).
3. Point them at [Getting started](Getting-Started.md) and the
   [documentation map](../README.md).

## Periodic maintenance

- Quarterly: review `uv.lock` majors (see [Dependencies](#dependencies)), drop
  unused dev dependencies (AEP 10).
- Per release: run [the release checklist](Release-Process.md).
- On every ADR: confirm `docs/adr/README.md` index and any guide it invalidates.
- Annually: verify a Raspberry Pi 4 / Raspberry Pi OS 64-bit install still works
  from the documented manual path (SAPRS 13.2 says the manual path is
  authoritative).

## Destructive actions, for the record

Deleting a label, rewriting `main` history, force-pushing a tag or closing a
milestone as "won't do" each need two maintainers and a note in
`CHANGELOG.md` under **Unreleased**. Anything that changes the fixed directory
layout needs an ADR (PBK 2).
