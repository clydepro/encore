# Release Process

## Versioning (PBK Chapter 17)

Semantic Versioning, `MAJOR.MINOR.PATCH`, tagged `vX.Y.Z`:

- **MAJOR** — incompatible appliance behaviour or public API changes
  (breaking changes are answered in the PR template and need migration notes).
- **MINOR** — backwards-compatible capability, mapped to the SAPRS 16 roadmap.
- **PATCH** — fixes only; no new behaviour.
- `0.x` is pre-release development; `1.0.0` is the first stable appliance
  release, i.e. SAPRS 16.2 complete.
- Pre-releases use `v1.0.0-rc.1`; the `release.yml` workflow skips PyPI
  publishing for any tag containing `rc`.

`pyproject.toml`, `encore.__version__` and the tag must agree — CI enforces it, in
`Release / verify`.

A release heading in `CHANGELOG.md` must read `## [X.Y.Z] - YYYY-MM-DD`, brackets
and ISO date included. `release.yml` greps for exactly that shape, so a heading
written as `## 1.0.0 - 2026-01-01` fails the release *before* anything is built or
published; `tests/unit/test_test_harness.py` checks existing headings against the
same rule so the surprise happens in a PR instead.

## Where releases happen

Everything below is automated by
[`release.yml`](../../.github/workflows/release.yml); the checklist exists so a
human knows what "done" means.

## Release checklist

### 1. Scope

- [ ] Milestone closed in GitHub; no `P0`/`P1` issues open.
- [ ] Every merged PR since the last release has a `CHANGELOG.md` entry. The one
      exception is Dependabot's dependency bumps: those are summarised by the
      generated release notes and by the `uv.lock` diff, and adding a line per
      lockfile PR is how changelogs become noise.
- [ ] `docs/adr/README.md` index matches `docs/adr/`.

### 2. Validation

- [ ] `scripts/check.sh` passes from a clean clone.
- [ ] `scripts/check.sh --slow` passes, including Party Simulation at the
      `baseline` profile (SAPRS 14.11).
- [ ] `uv lock --check`, `uv run pre-commit run --all-files` and
      `uv run mypy` are clean.
- [ ] No dependency vulnerabilities in the Security workflow since the last
      commit.

### 3. Platform

- [ ] Manual install followed exactly on Raspberry Pi OS 64-bit (this is the
      authoritative path, SAPRS 13.2).
- [ ] `encore-install` automates the same steps on Debian 12 and Ubuntu 24.04.
- [ ] systemd unit starts on boot, survives `systemctl restart`, and recovers
      when mpv is killed during playback (SAPRS 7.6).
- [ ] Performance targets met on real hardware (SAPRS 1.8) — recorded, not
      asserted.

### 4. Documentation

- [ ] `CHANGELOG.md`: `## Unreleased` becomes `## [X.Y.Z] - YYYY-MM-DD` (the
      brackets are what `Release / verify` greps for).
- [ ] User guide and administrator guide updated for visible changes.
- [ ] API reference updated; a MAJOR release includes migration guidance and a
      MINOR release documents new fields (SAPRS Chapter 10).
- [ ] Troubleshooting covers new failure modes introduced by the release.

### 5. Package

- [ ] `uv build` produces an sdist and wheel that install into a clean venv.
- [ ] `pyproject.toml` version bumped, `pyproject.toml` + `uv.lock` committed in
      one release-prep PR, and `metadata.version("encore")` agrees.
- [ ] `LICENSE`, `NOTICE` (if any) and asset licences reviewed.

### 6. Ship

```bash
git switch main && git pull --ff-only
git tag -s "vX.Y.Z" -m "Encore vX.Y.Z"
git push origin "vX.Y.Z"
gh run watch                      # release.yml runs verify → build → release
```

### 7. After

- [ ] Release assets exist (sdist, wheel) and the GitHub release notes read like
      a changelog, not a commit dump.
- [ ] Announce where the project announces things, linking the release.
- [ ] Milestone closed, next milestone opened, `Unreleased` section recreated.
- [ ] Any reverted or partially shipped feature recorded in an ADR.

## Hotfixes

1. Branch from the release tag, fix, add a regression test (SAPRS 14.14).
2. Bump PATCH, tag, let CI do the rest.
3. Forward-merge into `main` immediately; `main` is always deployable
   (SAPRS 15.11).

## Yanks and withdrawals

A published appliance release is never silently replaced. Superseded versions
are marked in `CHANGELOG.md` with the reason, the tag is retained, and operators
are told which version to move to.
