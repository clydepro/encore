# Handoff — after AIG step 15 (the installer)

For whoever picks this up next. Read with
[`current-phase.md`](current-phase.md) (what was done) and
[`context/milestones.md`](context/milestones.md) (what exists). Precedence is unchanged: task
request → SAPRS → AIG → ADRs → AEP (AEP 2). This page is a pointer, not an authority, and it will
be wrong faster than the SAPRS is.

State as of this writing: **Phases 1–5 are merged (#20, #22, #24, #26, #27). Phase 6 is
implemented and in review as [#33](https://github.com/clydepro/encore/pull/33), on
`feat/32-installer`, for issue #32 — CI green, not yet merged.** The appliance now installs itself:
`sudo ENCORE_ADMIN_PASSWORD='…' ./scripts/encore-install install`, from a clone, on a machine that
had nothing on it a minute before. A guest scans a code printed by the appliance itself, an
operator's password can be set before the box boots, and `encore-install verify` answers SAPRS
13.11's acceptance list with a fix under every line that is not green.

1491 tests pass in the standard gate, 93.5% coverage of `encore/`, `ruff` and strict `mypy` clean
across `encore/`, `apps/`, `tests/` and `tools/`. Two new ADRs (014, 015) and one new regression
file.

**Nothing here has met a Raspberry Pi.** The sentence was written in the last handoff about the
guest interface and about the administrative interface; now it is about the install itself, and it
is a sharper problem, because everything in phase 6 is *about* a machine and every test in it runs
on someone's laptop. The installer's host is a seam and its filesystem is real, so what is proven
is that the plan and the bytes are right — not that `audio` group membership means what Debian
thinks it means, or that `systemd-tmpfiles` behaves the way `tmpfiles_text` assumes, or that a Pi 4
with 1 GB of RAM resolves this dependency graph inside a timeout. **Run the manual steps on
hardware once before calling step 15 closed**, and file what surprised you; that checklist is
`docs/Administrator-Guide.md`'s installation section, which the installer automates and which is
authoritative in a way no test can be.

## Start here

1. **`docs/adr/ADR-014-*.md`** — why the installer is a third application rather than a bash
   script, a module of the Server, or a wheel someone downloads first; and why the repository layout
   grew a directory, which AIG 5 says needs an ADR to happen.
2. **`apps/installer/layout.py`** — the only place in the package where an absolute path appears,
   and the table the removal scopes and the deletion safety check are both derived from. Read this
   before `plan.py`; everything else is commentary on it.
3. **`apps/installer/plan.py`** — what the installer believes it is doing, as pure data. No
   effects, no `exists()` calls, one function per lifecycle command. If you want to know whether a
   second install does nothing, this is where the answer lives and `tests/unit/test_installer_plan.py`
   is where it is asserted.
4. **`apps/installer/execute.py`** — the only module that changes anything, and the one that
   refuses a deletion outside `Layout.owned_roots`.
5. **`docs/Administrator-Guide.md`'s installation section** — the manual steps, which are the
   specification the installer is measured against. Where the two disagree, one of them has a bug
   and it is not necessarily the installer's.

Then `tests/integration/test_installer_lifecycle.py`, which is this phase told as assertions, and
`tests/e2e/test_installed_appliance.py`, which installs an appliance and then boots it.

## Things that will surprise you

- **The plan is pure and the state is an argument.** `plan.py` never touches the filesystem, and
  the first draft did — four `Path.exists()` calls in the removal planner. They are now
  `ApplianceState.present`, populated by `system.observe()`. The reason is not aesthetics: a plan
  you can compute in a unit test is a plan you can review, and `--dry-run` is a lie if the planner
  can see a file the executor cannot.
- **`--root` renders staged paths into the files it writes**, so a staged tree is bootable rather
  than merely present. That is how the e2e test works and it is also a safety property: a staged
  unit file pointing at `/var/lib/encore` would be a test suite that writes to a production
  database. `Layout.staged()` is idempotent for the same reason — a path already under the root is
  returned unchanged — or the second install nests the prefix inside itself.
- **A preserved configuration is reported by name, not respected silently.** An existing
  `config.yaml` wins over every flag (13.8) and the run says `server.port is 9100 there, 8099
  asked for`. The comparison is of *parsed values*, so a comment somebody added is not drift. The
  distinction took two attempts to get right: bytes-made-equal hid real edits, and comparing the
  file against the in-memory model made every staged install report a hundred path differences.
- **`install --dry-run` is the plan command.** There was a sixth command, and it was removed: a
  separate `plan` duplicates what install does and drifts from it, which is the failure mode this
  whole phase is about.
- **`--dry-run` does not need `--yes`.** 13.10's guard is about performing a deletion, and a rule
  that blocked the rehearsal would only teach people to run the real thing to see what it says.
- **`ENCORE_ADMIN_PASSWORD` is read by the Server, not by the installer.** The installer writes it
  into `/etc/encore/environment` (mode `0600`) and that is the whole of its part; the appliance
  seeds its own credential on first boot, on the appliance thread, because it is a write to
  `runtime.db` and ADR-012's rule is what makes the rest of startup safe to reason about. A root
  process inserting rows into a database the service owns is how an install ends with a `runtime.db`
  whose journal is owned by root. `reset-credential` is the deliberate exception, and ADR-014 lists
  it as debt.
- **The QR code's URL is owned by one property.** `ServerConfig.guest_url` is read by `/qr`,
  `/api/v1/info`, the page footer and the installer's report. There is no `?url=` parameter, and
  there never will be — a QR endpoint that encoded a caller's string is an appliance that will
  print a phishing code about itself, and the page is public on purpose.
- **The QR page is public and cached, and both are arguments rather than oversights.** The address
  is already on every guest page's footer; behind the session cookie it would be a code only the
  operator can print. And SAPRS 10.10's two cache rules ("never cache mutable state", "cache
  artwork") both misfit it: a code is immutable until a restart, which is the lifetime of an asset.
- **The wheel is a deliverable and the test suite cannot see it.** `test_packaging.py` builds the
  artifact and opens it. Everything in `pyproject.toml`'s `packages` list is checked against the
  tree rather than against a list, so adding a package forces the same decision here. Note the
  namespace exception: `apps` has no `__init__.py` on purpose and the test says why.
- **`segno`, not a hand-rolled encoder.** A QR symbol is data encoding, Reed-Solomon over GF(256),
  mask selection by penalty score and a format string, in that order, and a mistake in any of the
  four produces an image that looks correct and does not scan — which surfaces at a party. The
  dependency is pure Python, BSD, vendored nothing. The unit test asserts byte-equivalence with
  what `segno` itself writes plus the standard module-count formula, because this segno version
  ships no decoder and a raster round trip would drag Pillow into a test of a URL.

## If you pick up step 16 (Party Simulation)

- The installer's seams are the model for "test the thing that touches a machine": a pure planner,
  an executor behind an interface, and a host that is an argument to `main()`. A simulation driver
  wants the same shape — a scenario in, a ledger out — and `tests/party_simulation/` is closest to
  `apps/installer/plan.py` in spirit than to anything in `encore/`.
- `tests/integration/test_real_mpv.py` skips where mpv is absent and is the only place a real
  player is driven. A party of 400 guests against a real mpv is a different test and it is allowed
  to be slow and opt-in.
- The 15,000-song corpus generator exists (`tests/support/`, used by the performance suite). What
  does not exist is a scenario with hundreds of concurrent HTTP clients, which is the half of
  milestone 16 the current simulations cannot fake their way through.
- Coverage: `apps/installer` is not in coverage's `source`, and neither is `apps/server`. If that
  is wanted, decide it in `docs/Developer/Testing.md` rather than by editing `fail_under` silently.
