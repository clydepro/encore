# ADR-014: The Installer Is a Third Application, and the Repository Layout Grows a Third Directory

## Status

Accepted

## Date

2026-11-06

## Context

SAPRS 13.7 asks for "a single command, `encore-install`, that performs steps 1 to 8 of the manual
installation, is idempotent, and reports next steps". AIG 21 lists the installer as milestone 15,
and AIG 23 lists it as a deliverable. AIG 5 fixes the repository layout at two applications —
`apps/builder/` and `apps/server/` — and neither of them is the right host for it.

The constraint that decides the shape is ADR-001, which is not primarily about processes. It is
about the fact that a program which builds a library must never be able to start playback, and a
program which plays music must never be able to rewrite the index. ADR-008 then makes the appliance
one artifact, and SAPRS 13.2 makes the manual steps in `docs/Administrator-Guide.md` authoritative:
the installer automates documented work, so a divergence between the two is a defect in one of
them.

Seven ways to build this were considered.

**A shell script in `scripts/`.** The obvious answer and the wrong one. The installer has to
validate `config.yaml`, and `encore/config/models.py` already contains the only correct definition
of a valid configuration (SAPRS 12.5's "report every offending setting at once" is thirty lines of
Pydantic validators). A bash installer would re-implement that in `grep` and `sed`, and the first
time a validator changed, the appliance and the installer would disagree about whether a machine
was installable — the failure SAPRS 12.5 exists to prevent, reintroduced through the tool that is
supposed to catch it. The other reason is Chapter 14: an installer that cannot be unit-tested is
an installer whose behaviour on a machine without systemd is discovered by somebody's party.

**A Python script in `scripts/`, not packaged.** Imports `encore.config`, so it solves the first
problem, and creates a second: `scripts/` is not an installable surface, so `encore-install` would
exist only inside a git clone. "One command from a clean SD card" would become "clone the
repository, create a virtual environment, then run a script" — which is the manual install wearing
a costume.

**A module inside `apps/server/`.** Rejected by ADR-001 and by the arithmetic of what it imports.
`apps/server/appliance.py` opens both databases, launches mpv and starts a thread. An installer
that imports it imports a sound card and a SQLite write handle into the process that is deciding
whether to delete `/var/lib/encore`. It also breaks AIG 4's rule from the other side: the installer
would then know the Server, and the Server would be one import away from knowing how to format a
`systemd` unit.

**A module inside `encore/`.** `encore/` is the runtime: everything under it is loaded by the
process systemd starts. Placing installer code there makes it importable by a controller, and a
route that can `chown` a directory is a privilege escalation with documentation attached. AIG 4's
guardrails are written as import rules precisely because "don't call that from a request" is not a
rule anyone keeps.

**A separate distribution (`encore-installer` on PyPI).** Two version numbers to keep in step, two
issue trackers, and a wheel that must be downloaded before the product can be installed — which is
the trust posture of a curl-pipe and the operational posture of a machine with no network. Encore
ships one artifact.

**A `postinstall` hook in the packaging metadata.** Rejected out of hand. It runs as whoever ran
`pip`, in an environment the packaging tool owns, with no opportunity to ask about `--domain`, no
`--dry-run`, and no way to be re-run. The lifecycle SAPRS 13.7 to 13.10 describes is five commands
with an exit-code contract; a hook is one unrepeatable side effect.

**Ansible, or a provisioning system.** Right domain model, wrong dependency. An appliance that
requires a configuration-management tool to install itself has gained a second appliance.

## Decision

`apps/installer/` is the third application in the repository, shipped as the `encore-install`
console script, and it is short-lived by design.

Five rules make it safe to have three.

**It imports inward, and never sideways.** `apps/installer` may import `encore.config` and
`encore.utilities`, and nothing else under `encore/`. It may not import `encore.playback`,
`encore.api`, `encore.controllers`, or `apps.server`. The first two are enforced by
`tests/unit/test_architecture_guardrails.py`, which also states the exception: importing
`encore.config` is a deliberate coupling, because two definitions of a valid `config.yaml` is a
worse defect than one dependency.

**Plan and execute are separate modules, and only one of them touches a machine.** `plan.py` takes
the observed state of a host plus what the operator asked for and returns an ordered list of
immutable `Action` values. It performs no effect and calls no existence check: `system.observe()`
has already asked every question the filesystem can answer, into `ApplianceState`. `execute.py` is
the only module in the package that changes anything. The consequence is that "what would this do?"
is answerable without doing it — which is what `--dry-run` is, and what a unit test of the
installer's behaviour on a machine without systemd is.

**The host is reached through two seams.** `CommandRunner` for commands, `Machine` for questions.
Everything else is filesystem, because the filesystem is what a test can share. `main()` accepts
both seams as keyword arguments, so an integration test drives the same nine steps the CLI drives
rather than reassembling them.

**Layout is data.** `layout.py` is the only place an absolute path appears, and it is the same
list of paths SAPRS 13.3 prints, including the removal scopes of 13.10. `owned_roots`, which is the
safety check that refuses a deletion outside the appliance, is *derived* from that table rather
than maintained beside it. Two lists — one for planning a deletion and one for allowing it — is a
setup where a directory appears in the first and not the second, and the failure is silent until
somebody restores a backup.

**The prefix is a staging root, not a `DESTDIR`.** `--root` redirects every documented path into a
self-contained tree, and the rendered files name the staged paths. This is the reason installer
tests can boot an appliance (`tests/e2e/test_installed_appliance.py`) rather than merely inspect it,
and it is why a rehearsal cannot write to a production database: a unit file pointing at
`/var/lib/encore` inside a staged tree would be the one way this suite could damage the machine
running it.

## Consequences

**Added.** The repository gains `apps/installer/` (eleven modules) and `tests/support/installer.py`;
`pyproject.toml` gains a third console script and two package declarations. AIG 5's layout is
amended by this document, as AIG 5 requires.

**Gained.** Milestone 15's deliverable is testable on a machine that is not a Pi: 133 unit tests
against the pure planner and the renderers, 29 integration tests against a real tree with a
scripted host, 9 end-to-end tests that install and then boot what they installed. Interrupted
installs are recoverable because recovery is a plan rather than a retry loop. The installer cannot
grow a second definition of a valid configuration without failing a test.

**Accepted.** Three applications to maintain, and a shared `encore/` whose import graph now has
three consumers with different privileges. The guardrail test is what keeps that from rotting, and
it is why this document names the rules rather than trusting the layout.

**Technical debt, recorded deliberately.**

- `reset-credential` opens `runtime.db` with `sqlite3` directly rather than through
  `encore.repositories.runtime`, because importing the repository layer into the installer would
  import the write path ADR-009 built for the appliance. The single `DELETE` is documented at its
  call site. If a third installer command ever needs a runtime write, this becomes the wrong shape
  and should be revisited as a `--maintenance` mode of the Server rather than as more SQL here.
- The venv stamp (`.encore-lock`) is a hash of a lockfile rather than a resolved-manifest
  comparison. It is correct for "did the dependencies change" and wrong for "did a dependency's
  build break", which `uv sync` would then not re-run.
- `--root` staging is applied to every path including `/usr/lib/tmpfiles.d`, which is harmless for
  a rehearsal and slightly fictional for a distribution package build. If Encore is ever packaged
  for a distro, the staging rule wants an escape hatch, and it should be an ADR rather than a flag.
- No rollback. A failure leaves the machine in the state the plan reached, and the report says so.
  SAPRS 13.7 asks for idempotence, which is the property that makes a re-run the recovery; a
  journal of performed actions with an undo path is a larger idea than a milestone and is not
  implied anywhere in the specification.

## References

- SAPRS 13.2 (the manual steps are authoritative), 13.3 (layout), 13.5 (service user), 13.6
  (systemd), 13.7 (the one command), 13.8 (configuration preservation), 13.9 (upgrade), 13.10
  (removal), 13.11 (acceptance list).
- ADR-001 (Builder and Server are separate applications — the argument this one generalises to a
  third), ADR-008 (appliance-first), ADR-009 (which database a process may write), ADR-013 (the
  credential this installer seeds), ADR-015 (what it seeds and what it prints).
- AIG 5 (layout, amended here), AIG 6 (the two applications, now three), AIG 4 (guardrails), AIG 21
  step 15, AIG 23.
- Issue #32. `docs/Administrator-Guide.md`, `tests/unit/test_architecture_guardrails.py`,
  `tests/integration/test_installer_lifecycle.py`, `apps/installer/`.
