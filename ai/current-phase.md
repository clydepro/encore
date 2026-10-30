# Current Phase

**Phase 6 — the installer (AIG step 15): merged** as
[#33](https://github.com/clydepro/encore/pull/33) for issue
[#32](https://github.com/clydepro/encore/issues/32), then fixed five times by the commands in its own
documentation ([#38](https://github.com/clydepro/encore/issues/38),
[#41](https://github.com/clydepro/encore/issues/41),
[#43](https://github.com/clydepro/encore/issues/43),
[#44](https://github.com/clydepro/encore/issues/44),
[#46](https://github.com/clydepro/encore/issues/46) — see "What an installed appliance found" and
the two "running it" sections below) — and a sixth time by the hardware it was installed
on ([#48](https://github.com/clydepro/encore/issues/48), "What an installed appliance heard": a
sound card the service account could not open, reported as a bad song and a healthy component), and
a seventh time by the artifact it publishes
([#51](https://github.com/clydepro/encore/issues/51), merged as
[#54](https://github.com/clydepro/encore/pull/54): an artwork cache addressed by the wrong digest,
which made a build over a real library impossible and left every tile grey). Verifying that on the
machine found two more — [#52](https://github.com/clydepro/encore/issues/52), an incremental rebuild
that publishes zero artwork, and [#55](https://github.com/clydepro/encore/issues/55),
`encore-install upgrade` deciding the code is already installed by comparing `pyproject.toml` — both
fixed on this branch, and #52's grey tiles turned out to have a third cause of their own, which is
what "What an appliance asked MusicBrainz about (#57)" below is about.
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
| A rehearsal that rehearses | `plan.registers_host_service`, `systemd_running`, `Plan.planning`; `test_issue_43_service_manager.py` (8) | SAPRS 13.7, #41, #43, #44 |
| An environment the service can actually execute | `Layout.python`, `interpreter_of`, `interpreter_blocker`, `Machine.resolve`; `test_issue_46_venv_interpreter.py` (9) | SAPRS 13.4, #46 |
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

## What an installed appliance found (#46)

Step 15 was run on a real machine for the first time — not staged, not `--no-service`, `install`
then `systemctl start encore` — and the appliance did not come up:

```text
encore.service: Failed at step EXEC spawning /opt/encore/venv/bin/encore-server: Permission denied
```

**#46 — the interpreter was in `/root`, and the acceptance list was green.** A console script names
the program the kernel actually runs, and `uv` had downloaded that program into the home of whoever
called it (Encore needs 3.12, Debian 12 ships 3.11). Under `sudo` that is `/root/.local/share/uv/`
`python`, behind a `drwx------` the account in `User=` cannot search. The venv is correct, its
entry point is `0755`, and `checks._environment` tested `st_mode & 0o111` — a true answer to the
wrong question, which is why `verify` printed fifteen OK lines over a service that could not start.
Three separate things were missing, and all three are in now:

1. **Placement.** `/opt/encore/python` (a `Layout` path, in `directories` and in the application
   removal scope) plus `uv python install --install-dir` and `uv sync --python <path>`. The env
   variable alone does *not* work: `uv` also searches a data directory beside its own binary, and
   #38 deliberately resolved that binary to the operator's `~/.local/bin`. Verified on the host —
   with `UV_PYTHON_INSTALL_DIR` set and the appliance directory populated, `uv sync` still relinked
   the venv to `/home/matt/.local/share/uv/python/…` until the interpreter was named explicitly.
2. **Observation.** `interpreter_of` (a shebang is a machine fact) and `interpreter_blocker` (the
   DAC walk the kernel makes, component by component, for *that* account), gathered once by
   `observe()`. `stat` is an `lstat` throughout this installer, so `Machine.resolve` is the
   separate question that follows the link — the second half of why no mode check could see this.
3. **Repair.** An unreachable interpreter counts as drift. Without that, the installer that fixed
   #46 would print 13.7's `nothing to do — the machine already matches this request` on every
   machine #46 had already broken, because the lockfile stamp matches perfectly well.

And one thing that was nearly wrong in the fix: the first cut made the finding a *prerequisite*
failure, so `install` refused to run — `a prerequisite is unmet, so nothing was changed` — over the
defect its own run repairs. It is a WARN on `install`/`upgrade` and a FAIL on `verify` now, which is
the distinction #43 drew, applied to the opposite error.

**The finding, for the fifth time.** #38 asked the wrong *account* about a path; #41 asked a
command that always answered yes; #43 inferred one fact from another; #44 stated a rule in two of
three places; #46 asked the right question about the wrong *subject* — the file, when the answer
belongs to the user. The general form: **`mode & 0o111` is not "can this be executed".** Any check
in this repository that ends in a permission bit is asking about an inode while the product's
question is about an identity, and the two differ by the whole path above the file.

## What an installed appliance heard (#48)

The appliance came up on the Pi after #46 and played nothing. Every request — mp3, m4a, whichever
guest asked — ended in the same WARNING, and `/health` was green:

```text
{"level":"WARNING","message":"mpv refused the file","song_id":900,
 "file":"/opt/music/.../04_Bad_Moon_Rising.m4a","detail":"audio output initialization failed"}
{"status":"healthy","components":[{"component":"playback","status":"healthy",
 "detail":"mpv running as pid 503668"}]}
```

**The sixth finding, and the one the unit file had already warned about.** Its comment says that
`PrivateDevices=yes` was avoided because it would give "an appliance in a sandbox with no sound
card" that "reports healthy and plays nothing". The sandbox was excluded correctly and the same
appliance happened anyway, because the device was wrong rather than hidden: `audio.device: default`
is ALSA's "whichever card registered first", which on a Pi 4 is the HDMI port, which had nothing
plugged into it. mpv's reply for that, and for a PulseAudio or PipeWire server the service account
cannot reach, is the same `file_error` it uses for a file it cannot demux, and Encore had one
handler for both: report the track, leave the machine alone. The general form: **a process being
alive is not the job being done.** #46's shape one layer down — a check that is true, about the
wrong subject, over an appliance that is not working.

What that took, in order of how much it mattered:

1. `PlaybackFault` (`encore/playback/fault.py`) classifies a refusal by mpv's own words, because
   nothing else in the reading distinguishes them — the properties are idle either way, which is
   the same fact that made `end-file` necessary in #23.
2. `PlaybackSupervisor.health()` reads it. `degraded`, with mpv's sentence and the two settings
   that decide which sound card it means, so the dashboard and the journal agree without anyone
   opening a manual. A corrupt *file* still costs the appliance nothing: a banner that is always
   red is a banner that gets ignored, and 15,000 songs contain some red. What the supervisor does
   not claim is that no sound is coming out of the room — `audio.device: default` on a Pi with a
   monitor on HDMI is a card that opens and plays, and the appliance cannot see the cable. It says
   the engine reported it could not initialize an output, which is the sentence a listener can act
   on.
3. A fault ends when a track plays, not when mpv restarts. That is the #23 lesson again — a
   reassuring event is not evidence — applied to the recovery path rather than the queue's.
4. `tests/support/mpv.py` gained the double the suite never had: an engine that is alive, answers
   IPC, and cannot make sound. `--ao=null` (which every real-mpv test here uses so it runs
   headless) cannot fail that way, so the new integration test points a real mpv at a device that
   does not exist and asserts the string; if a future build words it differently, that test says
   what the build said instead of the classifier quietly stopping to fire.
5. `content.config_text` now quotes `audio.device` and says what `default` resolves to. It used to
   say "`default` is right unless `amixer` says otherwise", which is a sentence that cost somebody
   an evening: `amixer` is asked about the wrong card and answers about it perfectly.

**What the fix does not cover, filed as
[#49](https://github.com/clydepro/encore/issues/49).** Rename `paths.music_root` and every request
still fails, but each failure is correctly a *file* refusal — so the queue keeps walking, health stays
green, and the appliance is silent for the same reason #48 was silent. The classifier is right about
every track and wrong about the run. That one needs a rate over real attempts (N refusals with no
successful play between them), which is a policy decision with a threshold in `audio:`-adjacent
configuration and an argument about which component owns the answer — not a change to slip into a
fix for a different machine fault. #48's own general form is what found it: re-read every health
source and write down the thing its component could not do while still saying `healthy`. The
`library`, `runtime` and `search` probes (`apps/server/appliance.py:444-495`) each name one, which is
why they are probes and not liveness checks; `library` checks the database, and never a file.

## What an installed appliance showed when it looked for a cover (#51, #52, #55)

The appliance came up, played, and every album was a grey tile. `encore-builder` had failed during
the install with `sqlite3.IntegrityError: UNIQUE constraint failed: artwork.relative_path`, and the
library that ended up published had **0** rows in `artwork` sitting on top of a cache of 222 JPEGs.

**#51 — the cache was keyed twice, and only one of the keys was the address.** `generate()`
deduplicated on the digest of the payload it *found* and `store_one()` named the file after the
digest of the bytes it *wrote*. Normalization decodes, resizes and re-encodes to one JPEG, which is
many-to-one: measured over this box's 3,049 files, **224 payloads produce 222 images**, because one
album's cover was tagged twice (a PNG differing from its eight siblings at byte 131) and another's
differed from its thirteen siblings by one trailing byte. Two assets, one path, and the `UNIQUE`
index was the first place in ten stages anybody noticed — inside the write transaction, with nothing
published. The module docstring had the rule right, `ArtworkOutcome.assets` had the contract right,
and the loop had a different key. `main()` then made it a traceback rather than an exit code, because
it catches `(OSError, ValueError)` and a database error is neither, in a function whose docstring
says it never raises for a build failure.

**Two more, found by verifying the fix rather than by writing it.**

**[#52](https://github.com/clydepro/encore/issues/52) — an incremental rebuild publishes zero
artwork.** `_extract()` reuses a cached `ProbeResult` per ADR-010; a restored `ProbeResult` carries
tags and duration and no `artwork_bytes` (SAPRS 5.6 forbids BLOBs, so nothing stored could hold
them); so every reused file reaches stage 5 looking like a file with no cover. `Reusable.artwork`
exists for exactly this, is documented — "so an unchanged build writes zero images" — is loaded by
`_fill_artwork` from `music_files.embedded_artwork_sha`, and is read by nothing. Seven files, two
builds: `artwork rows=2 → 0`, `cache files=2 → 2`, validation green both times. This is why the
documentation says a rebuild that is meant to change the pictures is `encore-builder --full`, and
why the repair on this box was `--full`.

**[#55](https://github.com/clydepro/encore/issues/55) — `encore-install upgrade` cannot see the code
it is asked to install.** Its dry run on this machine planned four steps, none of them a copy, and
the machine is two merged pull requests behind: `_source_matches` decides "the application already
holds the code being offered" by comparing `pyproject.toml`, byte for byte. That file has not
changed since the install and does not change for any commit that changes only Python. Sixteen
source files differed and the plan saw none of them, so #51 had to be deployed here by copying three
files by hand. `plan.py` describes the intended rule one line above the code that does not implement
it — "the files in `application/` match the ones being offered" — and `system.py` gives the reason
it must be observed rather than remembered: "an operator may have run `git pull` in the application
directory themselves". A `git pull` is the one event that changes no byte of the manifest.

**The finding, and it is the same one for the seventh time.** #38 asked the wrong account about a
path, #41 trusted a recorder that always said yes, #43 inferred one fact from another, #44 stated a
rule in two of three places, #46 read the file's mode when the question was the user's, #48 asked
whether the process was alive when the question was whether sound was coming out, and #51 keyed a
cache on the input when the address is the output. The form: **a digest is a claim about one
particular byte string, and every place a cache, a key or an identity is derived, the byte string
has to be the one the *consumer* will compare.** `relative_path` is what the Server opens, what the
UNIQUE index enforces and what the reference names; the payload was never any of those things. #55
is the same sentence about `pyproject.toml`: a manifest is not the tree, and an installer that reads
one to decide whether to copy the other is asking the file rather than the directory.

## What an appliance asked MusicBrainz about (#57)

The #52 fix landed, the appliance was rebuilt, and the tiles were still grey — which is a different
finding from #52 and a better one, because #52 was a stage losing information it had been given and
this was a feature that had never been asked to run.

**#57 — MusicBrainz had no configuration home, so it was never on.** Three defects, one sentence
apiece, and the appliance produced the evidence for all three.

1. **The default was a flag.** `--musicbrainz` existed in `apps/builder/main.py` and nowhere else:
   no key in `config.yaml`, nothing a systemd timer could see, so every unattended build printed
   `MusicBrainz: disabled`. The naive fix — a `builder:` section in the file — would have stopped an
   appliance booting over a correct configuration, because `EncoreConfig` is `extra="forbid"` and
   the Server validates the same file the Builder reads. So `BuilderConfig` lives in
   `encore/config/models.py`, with `musicbrainz: true` and `album_art: true`, and the guardrail that
   keeps the Server from acting on either is now a test:
   `test_the_builder_reaches_the_network_from_one_module_only`. Three CLI states (`BooleanOptionalAction`,
   so *not given* is neither `true` nor `false`) sit above two file states, and `--offline` outranks
   both because it is a statement about this afternoon's network rather than about the library.
2. **"Present" was being read as "correct."** SAPRS 6.6's second level is "fill gaps *or correct
   implausible values*" and `_gaps` implemented the first clause only, so `artist: 01` — 34 files,
   a rip tool writing the track number into the artist field, sixty files grouped under an artist
   named 01 — and `date: 24` (2 files, printed as 24) were never questioned. Plausibility went into
   `normalization.py` beside `is_placeholder`, which is where it can act with the network off: a
   counter artist falls to ADR-010's path level, and MusicBrainz is the second answer. Narrow on
   purpose (a field that is *nothing but* a small integer, artist fields only — 21, 25, 30 and 13 are
   albums), and it questions a value rather than discarding it: `_resolve`'s last resort still stands,
   so a band named 1776 keeps the name. A counter is corrected but never searched *with*, because
   `artist:01 AND recording:"Morning"` matches a hundred releases and the client takes the first.
   `NORMALIZATION_RULES_VERSION` went to 2 for exactly this reason: the old conclusions were wrong,
   and ADR-010 already priced the rebuild.
3. **SAPRS 6.7 named one artwork source and stopped.** FLAC carries no embedded picture, so 463 of
   703 albums were grey and validation was content — a missing picture is legal. ADR-016 adds the
   second level: the release's front image from the Cover Art Archive, asked once per album
   (`Track.group_key`, the same key `construction` groups by, so 926 requests rather than 1,989) and
   attached only when *both* names match a release exactly. Not closest, not highest-ranked, not the
   only result. A wrong cover is a lie about where something is; a grey tile is only a gap.

**And the half that was #52 again.** The reuse path sat inside the `if options.enrich` gate, so
`--offline` and `--no-musicbrainz` — the *careful* options — rewrote every previously enriched value
back to the tags and let the library decay in silence, which is 6.6's "shall not be degraded to
un-enriched data" broken by the flag that was supposed to honour it. Asking nothing is now free and
forgetting is not. Two more fields had #52's shape — a documented cache with no reader:
`albums.musicbrainz_release_id` was written by nobody and `PreviousEnrichment.release_id` was loaded
by nobody, so an album identified in March was searched for again in October; both are wired end to
end, and the second identification pass costs one request per album instead of two.

The eighth instance of the standing finding, after #38, #41, #43, #44, #46, #48, #51 and #55: **a
feature whose only switch is typed in a terminal is off, and a default nobody can read in a
configuration file is a default that has never been examined.** `MusicBrainz: disabled` printed on
100% of this appliance's builds without one person choosing it, and the report line nobody could
account for was the same document that said the library was healthy.

## What running it on a machine with systemd found (#43, #44)

The first `sudo encore-install install` — not staged, not `--no-service`, on a host with systemd as
PID 1 — exited 3 with `FAIL service manager: inactive`, above a report whose opening line was
`nothing to do`. Step 15's documented command had now been run in three of its four shapes: staged,
`--no-service`, and neither. Only the last one was the appliance.

**#43 — the check answered the wrong question.** `ServiceState.detail` held both "there is no
systemd here" and "here is what systemctl said about the unit", and `checks.py` decided the
prerequisite by which one was non-empty. On a first install the unit's absence prints `inactive`,
so a healthy Pi was declared unusable and the advice was `--no-service`, which is the option that
leaves a box that never comes back after a power cut. The fact the check needed was already being
observed inside `RealMachine.service()` (`/run/systemd/system`) and then thrown away; it is now a
field of its own, `ApplianceState.systemd_running`, asked of the machine separately. A machine with
no systemd still refuses, and still correctly.

**#43's second half was the prose.** An empty `Plan` meant three things — 13.7's idempotence (a
result), `verify` (never plans), and "we stopped before planning" — and the report printed all
three as the first one. `Plan.planning` now names which, in text and as `"planning"` in `--json`.

**#44 is what fixing #43 let anyone reach.** `--root` ran `systemctl daemon-reload`, `enable`,
`start` — and in `upgrade`/`remove`, `stop` and `disable` — against the *host*, because `systemctl`
cannot be aimed at a prefix. So a rehearsal of an upgrade silenced the appliance people were
listening to while copying files into a directory that has nothing to do with it. Staging already
declined to run `useradd` for exactly this reason, and `next_steps` already refused to print
`systemctl start encore` for a rehearsal; the plan was the third copy of the rule that had not been
told. One predicate decides it now (`plan.registers_host_service`, all four commands), and the
rehearsal reports the command to run on the real machine instead of quietly skipping it.

**The finding, five times over.** #38 was an observation the fake could not make about a path, and
issue #41 a command the recorder always answered yes to; #43 was a *combination* the type could not
express (`systemd=True` implied `detail=""`, so no test could say "systemd here, and no unit yet"),
issue #44 a rule stated in two of three places, and #46 the wrong *subject* in the question — the file's
mode, when the answer belongs to the user. Each one was invisible to the same 1500 tests for the
same kind of reason, so the rule for anything new that touches a machine: name the identity the
claim is about, ask it once in `observe()`, and if two questions can disagree on a host they get two
fields. `docs/Developer/Testing.md` has the wording, and the regression files for #43 and #46 assert
against the real `systemctl` and the real kernel rather than against a double's opinion.

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
   before calling step 15 closed. #38, #41, #43 and #44 were what the attempts at that found — four
   failures, all four invisible to 1500-odd tests — and there will be more where those came from.
   On the Pi, run the four shapes, not one: `install` as root, `install --root /tmp/x` as root,
   `install --no-service`, and `verify` after each. The second one is the #44 assertion, and it is
   the easy one to skip: after it, `systemctl is-active encore` must answer the same thing it
   answered before. Then check what #46 was, because no staged test can: `sudo -u encore
   /opt/encore/venv/bin/python -c 'print(1)'` must print, and `namei -l "$(readlink -f
   /opt/encore/venv/bin/python)"` must not stop in anybody's home. Run
   `pytest tests/regression/test_issue_38_installer_path.py tests/regression/test_issue_46_venv_interpreter.py`
   as root while you are there: those are the files that ask the machine to be a machine, and off
   hardware their root-only tests never run at all.
2. **Party Simulation driver (milestone 16)** — the 15,000-song corpus, hundreds of guests,
   HTTP-fronted clients. `tests/party_simulation/test_admin_in_the_room.py` added the operator to
   the shape of a party; the scale is still missing.
3. **Coverage floor** — raise `fail_under` deliberately, with the number in
   `docs/Developer/Testing.md`, rather than letting it drift up as a side effect. The installer's
   code is under `apps/`, which the coverage `source` does not include; if that is wanted, say so
   in `docs/Developer/Testing.md` rather than by editing the config silently.
4. **`--root` as a packaging convention.** Staging is DESTDIR-like and applies to every documented
   path, including `/usr/lib/tmpfiles.d`. If Encore is ever packaged for a distribution, that rule
   wants an escape hatch, and ADR-014 says it should be an ADR rather than a flag. The rule now has
   a name (`plan.registers_host_service`) and one exception's worth of evidence behind it (#44);
   anything else that asks PID 1 to do something belongs behind that predicate too, and
   `install --source` is the second case, for the reason 13.7's staged note already gives.
5. **`reset-credential`'s direct SQL.** It is the installer's one write to `runtime.db`, documented
   in ADR-014's debt list. If a second runtime write is ever needed here, the shape is wrong and
   the answer is a maintenance mode of the Server, not more SQL in the installer.
6. **`GET /admin/sessions`?** — carried forward from phase 5, unchanged.
7. **MusicBrainz is on by default, and that is new behaviour on an upgraded appliance.**
   `builder.musicbrainz` and `builder.album_art` are both `true`, so a build after this upgrade
   contacts musicbrainz.org and coverartarchive.org at one request per second, and
   `NORMALIZATION_RULES_VERSION` 2 makes the first one a full re-read. Administrator-Guide says so
   in both places an operator looks (the flag table, and the security posture), and
   `musicbrainz: false` is the whole of opting out — an offline build now keeps everything an online
   one ever learned, which is what made the default safe to change. Two debts are named rather than
   hidden: the Cover Art Archive asks for a credit line and the UI does not render one yet
   (ADR-016's consequences), and a stale release identifier is reused until the tags change or
   somebody runs `--full`, which is the cache working and the sentence an operator should hear in
   those words.
   [#52](https://github.com/clydepro/encore/issues/52) and
   [#55](https://github.com/clydepro/encore/issues/55) are fixed on this branch; the third
   open question is whether `apps/` should be inside the coverage `source` at all, which item 3 asks
   for a decision on rather than a drift.
