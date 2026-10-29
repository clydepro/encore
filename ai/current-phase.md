# Current Phase

**Phase 6 — the installer (AIG step 15): merged** as
[#33](https://github.com/clydepro/encore/pull/33) for issue
[#32](https://github.com/clydepro/encore/issues/32), then fixed twice by its own documented command
([#38](https://github.com/clydepro/encore/issues/38),
[#41](https://github.com/clydepro/encore/issues/41) — see "What running it as root found" below).
**Phase 7 — the dependency-alert chore — merged as
[#40](https://github.com/clydepro/encore/pull/40), closing
[#39](https://github.com/clydepro/encore/issues/39).** Phases 1–5 are merged:
[PR #20](https://github.com/clydepro/encore/pull/20) (domain, bus, configuration),
[PR #22](https://github.com/clydepro/encore/pull/22) (repositories, Builder — closed #19),
[PR #24](https://github.com/clydepro/encore/pull/24) (search, playback, queue — closed #23),
[PR #26](https://github.com/clydepro/encore/pull/26) (HTTP, HTMX, SSE — closed #25) and
[PR #27](https://github.com/clydepro/encore/pull/27) (administration — closed #28).

This phase followed CONTRIBUTING §2 in order for the first time: issue #32 was opened, then the
branch was named for it, then the code was written. The lesson from phase 5's backwards branch
(`feat/27-…`, named for the pull request) is recorded in the previous version of this file.

## Delivered

| AIG 15 requirement | Delivered as | Spec |
| ------------------ | ------------ | ---- |
| One command, idempotent, reports next steps | `apps/installer/main.py`, `plan.py`, `execute.py`, `report.py`; `scripts/encore-install` for the bootstrap case | SAPRS 13.7, ADR-014 |
| Filesystem layout, service user, groups | `apps/installer/layout.py` (the only place an absolute path appears), `system.py` | SAPRS 13.3, 13.5 |
| systemd unit, enable, tmpfiles rule | `content.unit_text`, `content.tmpfiles_text`, `_service_actions` | SAPRS 13.6 |
| Configuration, preserved if it exists | `config_file.py` (`decide`, `merge_existing`, `load_validated`), rendered by `content.config_text` | SAPRS 13.8, 12.5 |
| Upgrade with a rollback copy | `plan.upgrade_plan`, `execute.BackupRuntimeDatabase` (SQLite's backup API, not `cp`) | SAPRS 13.9 |
| Removal in three scopes | `layout.removals`, `plan.remove_plan`, `--purge-config`, `--purge-data` | SAPRS 13.10 |
| Acceptance list as a command | `checks.assess`, `encore-install verify`, exit code 4 | SAPRS 13.11 |
| The administrator credential | `AdminService.seed()` from `ENCORE_ADMIN_PASSWORD`, on first boot, on the appliance thread | SAPRS 12.6, ADR-013, ADR-015 |
| QR onboarding | `utilities/qr.py`, `controllers/qr.py`, `templates/pages/qr.html` — `GET /qr`, `GET /qr.svg` | SAPRS 9.11, ADR-015 |
| The address, owned by one property | `ServerConfig.guest_url`, read by `/qr`, `/api/v1/info`, the footer and the installer's report | SAPRS 1.4, 12.2 |
| An install that produces a working artifact | `pyproject.toml` (`packages`, `package-data`, three console scripts), `MANIFEST.in`, `tests/unit/test_packaging.py` | SAPRS 13.4, AIG 20 |

## The shape of it

**The installer is a third application, not a third layer.** `apps/installer/` sits beside
`apps/server/` and `apps/builder/`, ships as `encore-install`, runs once as root and exits. It may
import `encore.config` and `encore.utilities` and nothing else under `encore/`; the guardrail tests
enforce that, and ADR-014 states the argument. The reason it is Python rather than bash is that it
has to validate `config.yaml`, and `encore/config/models.py` already holds the only correct
definition of one — a bash installer would re-implement that in `grep`, and the first time a
validator changed, the appliance and the installer would disagree about whether a machine was
installable.

**Planning and doing are different modules.** `plan.py` is pure: observed state plus options in,
an ordered list of frozen `Action` values out, no effects, and not even a `Path.exists()` call —
`system.observe()` has already asked every question the filesystem can answer. `execute.py` is the
only module in the package that changes anything. This is what makes `--dry-run` honest, what makes
"what would this do?" a function call, and what lets 133 unit tests cover an installer's behaviour
on a machine without systemd, without pretending to have one.

**The host is reached through two seams.** `CommandRunner` for commands, `Machine` for questions
about users, groups, ports, systemd and OS release. `main(argv, *, machine=…, http=…)` accepts
them, so an integration test drives the same nine steps the CLI drives rather than rebuilding them
— which is the difference between testing the installer and testing a test author's memory of it.

**Idempotence is asserted as an empty plan.** `observe()` reads the machine; a second install
finds nothing to say. The integration tests prove this on bytes and mtimes, not on intentions: a
file rewritten with identical contents would pass a contents-only check and still restart a service
that had no reason to restart.

**Layout is data, and the safety check is derived from it.** `layout.removals` is one table naming
every path `remove` may delete and with which SAPRS 13.10 scope; `owned_roots` — the check that
refuses a deletion outside the appliance, and follows-no-symlinks on the way — comes out of the
same table. Two lists, one for planning a deletion and one for allowing it, is a setup where a
directory appears in the first and not the second, and the failure is silent until somebody
restores a backup.

**The configuration is never overwritten, and the disagreement is named.** An existing
`config.yaml` wins over every flag, and the run says so with the dotted setting and both values:
`server.port is 9100 there, 8099 asked for`. A file that is *invalid* is kept and the run fails,
because replacing something nobody can read destroys the evidence and the operator's intent in the
same act (SAPRS 12.5). The comparison is of parsed values, so a comment somebody added is not a
drift — that distinction is what makes a second install quiet.

**`--root` is a staging prefix, and the tree it produces boots.** Every rendered file names the
staged paths, so `tests/e2e/test_installed_appliance.py` can install an appliance and then start
it, reading the `config.yaml` and the environment file the installer wrote. That is also the
safety property: a staged tree whose unit file pointed at `/var/lib/encore` would be a test suite
that can write to a production database.

## Three findings that were not installer bugs

Milestone 15 was a program that did not exist; what turned up while building it were three defects
that each lived in a place no existing test looked, and that were all green before they were found.
They are in `tests/regression/test_issue_32_installer.py` and each had its own sentence in #32.

1. **The wheel had no server in it.** `[tool.setuptools] packages` listed `encore` and
   `apps.builder` and omitted `apps.server` and the two `encore.repositories.*` subpackages. It
   built cleanly, installed cleanly and passed 1253 tests, because pytest imports from the
   checkout. `tests/unit/test_packaging.py` now builds the wheel and opens it, comparing its
   contents against the source tree rather than against a list.
2. **Nothing read `ENCORE_ADMIN_PASSWORD`.** The Administrator Guide, SAPRS 12.6 and ADR-013's own
   forward note all described it; `AdminService` had a first-run page and a rotation endpoint and
   no way to arrive pre-configured. An operator who followed the documentation was told the
   password was not right, on the machine they had just installed.
3. **`/api/v1/info` built its URL by hand** — `f"http://{domain}/"`, on an appliance listening on
   8080. Three files formatted the address their own way and each was individually correct; the
   fix is one property, and a regression test that compares the four outputs rather than the
   inputs.

The first two are artifacts of the boundary between applications. That is worth stating plainly
for whoever picks up step 16: **a bug that lives between two packages is invisible to the test
suite of either one**, and the only cure is a test that crosses.

## What running it as root found (#38, #41)

Step 15 merged with the installer never having run in a root shell. It has now, twice, and it failed
twice — both times in the gap between the fake the tests used and the machine the code is for.

**#38 — `uv` is missing, on a machine that has it.** The astral installer writes `~/.local/bin`;
`sudo` replaces `PATH` with its `secure_path`, which names no home directory. `RealMachine.which`
searched `PATH` only, so the two documented instructions, followed in order, produce an installer
that tells you to install something you installed. The fix is one function — `search_path` — that
owns "where a host binary can be", asked by both the check and `SubprocessRunner`, because the
deeper bug was a question and a command resolving through two different lists: that version fails
with a 127 in the middle of a plan instead of a prerequisite message before it. Candidate
directories are appended, never prepended: a home directory that can shadow `/usr/bin/mpv` is a
privilege escalation waiting for somebody to install a tool. `scripts/encore-install` keeps a
three-line bash copy of the same list, because it has to find uv before any Python exists to ask —
the only duplication in the milestone, commented as such.

**#41 — `chown` to an account the rehearsal skipped.** `--root` plans no `useradd` (13.7), so
`chown root:encore` names a group that does not exist, and `Executor._ownership` decided fatality
from `os.geteuid()`: right about the process, wrong about the machine. The decision now comes from
observed state (`ApplianceState.root`, `.user_exists`), which is what that module's own docstring
always claimed. On a real appliance a failed `chown` is still fatal, and that half is asserted too —
the fix must not mean a machine that cannot read its own configuration shrugs.

**A third, found by running the suite as root rather than the installer.**
`test_a_real_root_rehearsal_completes` is the only test in this repository that CI has never
executed: it skips unless the process is root, and the runners are not. It was red the first time it
ran — its recorder was given `real=(("rsync",),)` and no `effects`, so `uv sync` was recorded,
produced no venv, and the acceptance list correctly failed the rehearsal it existed to bless. One
argument fixes it (`effects=synced()`, what every other installer test passes), but the reason it
was wrong is the same as #38's and #41's: the assertion lived in a branch of the machine nobody on
this project had standing in. A skip that has never been un-skipped is not coverage.

**The finding worth carrying into step 16.** `RecordingRunner` answered every unrecognised command
with success, so every test in the suite saw a `chown` that worked. Changing that one default turned
19 tests red. This is the general form of the sentence in `ai/HANDOFF.md` about bugs living between
two packages: **a double that agrees with whatever it is asked is not a check**, and the more of the
machine a suite stands in for, the more the fiction needs at least one way to say no. A party
simulator whose guests never fail is the same trap with a bigger number in front of it.

## What the next phase inherits

1. **A Raspberry Pi bring-up checklist.** The installer is tested against a real staged filesystem
   with a scripted host; what no test proves is sound reaching a speaker — ALSA device names, USB
   DACs, the mpv socket under the `encore` account on a machine with a real `audio` group. The
   manual steps in `docs/Administrator-Guide.md` are the checklist; run them once on hardware
   before calling step 15 closed. #38 and #41 were what the first attempt at that found, and there
   will be more where those came from. Run `pytest tests/regression/test_issue_38_installer_path.py`
   as root while you are there: it is the one file in the suite that asks the machine to be a
   machine, and off hardware one of its five tests never runs at all.
2. **Party Simulation driver (milestone 16)** — the 15,000-song corpus, hundreds of guests,
   HTTP-fronted clients. `tests/party_simulation/test_admin_in_the_room.py` added the operator to
   the shape of a party; the scale is still missing.
3. **Coverage floor** — raise `fail_under` deliberately, with the number in
   `docs/Developer/Testing.md`, rather than letting it drift up as a side effect. The installer's
   code is under `apps/`, which the coverage `source` does not include; if that is wanted, say so
   in `docs/Developer/Testing.md` rather than by editing the config silently.
4. **`--root` as a packaging convention.** Staging is DESTDIR-like and applies to every documented
   path, including `/usr/lib/tmpfiles.d`. If Encore is ever packaged for a distribution, that rule
   wants an escape hatch, and ADR-014 says it should be an ADR rather than a flag.
5. **`reset-credential`'s direct SQL.** It is the installer's one write to `runtime.db`, documented
   in ADR-014's debt list. If a second runtime write is ever needed here, the shape is wrong and
   the answer is a maintenance mode of the Server, not more SQL in the installer.
6. **`GET /admin/sessions`?** — carried forward from phase 5, unchanged.
