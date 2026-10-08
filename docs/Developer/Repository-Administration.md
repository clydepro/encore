# Repository Administration

One-time and periodic maintainer tasks (PBK Chapter 7, Chapter 10). All of them
are repository *settings*, which cannot live in the Git tree — so they are
written down and scripted instead.

Requires the GitHub CLI with admin access: `gh auth login`.

## First-time setup

```bash
scripts/sync-labels.sh                       # PBK 10 taxonomy
scripts/configure-branch-protection.sh       # PBK 7 rules for main
gh api -X POST "repos/$OWNER/$REPO/vulnerability_alerts"   # Dependabot alerts
```

Then, in the web UI (Settings), because there is no stable public endpoint for
them on all plans:

1. **Settings → General → Pull Requests**
   - Allow merge queue (optional, recommended once contributors overlap).
   - Automatically delete head branches ✅.
   - Squash merge optional; `main` requires linear history either way.
2. **Settings → Code and automation → Code security and analysis**
   - Secret scanning ✅ and push protection ✅.
   - CodeQL (code scanning) ✅ — required before `security.yml` can upload SARIF.
   - Dependabot version updates ✅ (configuration is already committed).
3. **Settings → Branches → Branch protection rules**
   - Verify `main` matches [`.github/branch_protection.json`](../../.github/branch_protection.json);
     re-run the script rather than editing by hand.
4. **Settings → Environments**
   - Create `pypi` with required reviewers if/when packages are published.
5. **Signed commits** (optional but recommended)
   - Add a second, "require signed commits" rule for release branches, and ask
     maintainers to configure SSH or GPG signing.

## Required checks

The `contexts` list in `.github/branch_protection.json` names workflow jobs
exactly as they appear in the Checks tab. After changing a job name, re-run
`scripts/configure-branch-protection.sh`, otherwise PRs wait forever on a check
that will never come.

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
scripts/sync-labels.sh --prune --dry-run   # review drift first (see tools/sync_labels.py)
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

- Quarterly: review `uv.lock` majors, drop unused dev dependencies (AEP 10).
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
