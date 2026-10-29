# Administrator Guide

Status: **implemented** — the administrative interface landed with milestone 14
(AIG 21, #28), and this page describes the thing that exists rather than the thing
that was planned. Written for the person who installs the box, and who will be
asked about it at a party.

## Mental model

Two programs do the work, a third sets them up, two databases hold the results,
and one appliance runs them:

```text
music files → Library Builder → library.db (immutable) → Encore Server → mpv → speakers
                                            runtime.db (queue, history, stats) ↗
```

The Installer is where the arrow starts. It runs once, as root, arranges the
filesystem and the service, and exits; it is not part of the appliance and the
Server never runs it (ADR-014). You build the library elsewhere, publish the
database, then run the server and forget about it (ADR-001, ADR-006, ADR-008).

## Installation

One command, on the machine that will play the music:

```bash
git clone https://github.com/clydepro/encore.git && cd encore
sudo ENCORE_ADMIN_PASSWORD='a password you will remember' ./scripts/encore-install install  # pragma: allowlist secret
```

It creates the service user, lays out the filesystem, installs the code and its
dependencies, writes `config.yaml`, the environment file, the systemd unit and
the tmpfiles rule, then starts Encore and tells you what to do next. Run it
again and it does nothing — that is the property everything else about it
depends on (SAPRS 13.7).

The manual steps below are the authoritative ones (SAPRS 13.2); the installer
automates exactly this sequence, and where the two disagree the manual steps are
the bug report. Do them by hand if you would rather see what the machine is
being told, if you are packaging for a distribution, or if you do not trust a
program you did not read, which is a reasonable position about software that
runs as root.

**What the machine needs first:** `mpv`, and `uv`. If you followed
[Getting Started](Developer/Getting-Started.md) to install `uv`, that script puts
it in `~/.local/bin` and `sudo` does not carry that directory into root's `PATH`.
The installer looks in both accounts' `~/.local/bin`, `~/bin` and `~/.cargo/bin`
as well as your `PATH`, and prints the list when it cannot find anything, so a
"uv is missing" message tells you whether it is missing or merely somewhere root
cannot see (#38).

**Target:** Raspberry Pi 4 on Raspberry Pi OS 64-bit, Debian 12 or Ubuntu 24.04.

### The one-command installer

| Command | What it does |
| ------- | ------------ |
| `install` | Everything above. Idempotent; re-running repairs a half-finished install |
| `upgrade` | Stop, replace the code, back up `runtime.db`, start. Your configuration, library, queue and history survive |
| `verify` | SAPRS 13.11's acceptance list, printed with a fix for every line that is not green. Changes nothing |
| `remove` | Deletes the application. Keeps the configuration, the databases and the music unless you say otherwise |
| `reset-credential` | Clears the operator credential so the first-run page comes back. Needs `--yes` |

Flags worth knowing:

- `--dry-run` prints the plan and changes nothing. `remove --dry-run` does not
  need `--yes`, because describing a deletion is not one.
- `--root /prefix` rehearses the whole install into a self-contained tree. Every
  rendered file names the staged paths, which means the tree is bootable, which
  is how the test suite proves an installed appliance works (`tests/e2e`). It also
  skips `useradd`, so a rehearsal run as root reports `ownership not set (no
  'encore' account in a staged install)` and exits 0 — that line is the rehearsal
  describing itself, not a machine fault (#41).
- `--no-service` skips systemd. For containers, and for the rehearsal above.
- `--domain jukebox.home.arpa`, `--port 8080`, `--music-dir /opt/music`,
  `--hostname jukebox`, `--log-format json|console`.
- `--json` for a provisioning system: the plan, the results, the acceptance
  list, and `guest_url`, in one machine-readable object.

Exit codes: `0` worked, `2` the command line was wrong, `3` a prerequisite is
unmet (fix the machine, run it again), `4` the machine is fine and something
about Encore is not. Warnings never change the code — an appliance with no
library is correctly installed and waiting, not broken.

`ENCORE_ADMIN_PASSWORD` is read from the environment and has no command-line
equivalent on purpose: a flag would put the appliance's only credential in every
user's process list and in somebody's shell history (SAPRS 12.6). It is used
once, on the appliance's first boot, and never overwrites a password an operator
has already set (ADR-015).

### The manual steps

1. Create the service account and the groups (SAPRS 13.5):

   ```bash
   sudo useradd --system --home-dir /var/lib/encore --shell /usr/sbin/nologin encore
   sudo usermod -aG audio encore
   ```

2. Create the layout of SAPRS 13.3, with the ownership and modes of 13.5:

   ```bash
   sudo mkdir -p /opt/encore /etc/encore /var/lib/encore/artwork \
                /var/cache/encore/temp /var/log/encore
   sudo chown root:encore /opt/encore /etc/encore /var/cache/encore
   sudo chown encore:encore /var/lib/encore /var/log/encore
   sudo chmod 750 /var/lib/encore /var/lib/encore/artwork /var/log/encore
   sudo chmod 700 /var/cache/encore/temp
   ```

3. Install the code and its dependencies into a dedicated environment. Nothing
   goes into system Python (SAPRS 13.4):

   ```bash
   sudo git clone https://github.com/clydepro/encore.git /opt/encore/application
   sudo -u encore UV_PROJECT_ENVIRONMENT=/opt/encore/venv \
        uv sync --frozen --no-dev --project /opt/encore/application
   ```

4. Write `/etc/encore/config.yaml` from
   [`examples/config.yaml`](../examples/config.yaml), and
   `/etc/encore/environment` at mode `0600` with `ENCORE_HOME`, `ENCORE_CONFIG`
   and, if you want the box configured before it boots, `ENCORE_ADMIN_PASSWORD`.

5. Install the unit (SAPRS 13.6) and the tmpfiles rule that keeps the mpv socket
   directory from being cleaned away, then `systemctl daemon-reload`, `enable`
   and `start encore`.

6. Publish a library. The installer does not do this for you: it needs a music
   directory that only you know the location of (ADR-010).

   ```bash
   encore-builder --music-dir /opt/music
   ```

7. Open `http://encore.local:8080/admin`, check that health is green, and print
   the QR code at `/qr` (SAPRS 9.11). If `ENCORE_ADMIN_PASSWORD` was set the
   credential already exists; if not, the first-run page asks you to create one.

## Building the library

The Builder is a separate, offline command. It is the only program that reads
your music files and the only thing that ever writes `library.db` (ADR-010);
the Server opens that file read-only and never creates it.

```bash
encore-builder                      # uses paths.* from /etc/encore/config.yaml
encore-builder --music-dir /mnt/library --dry-run
```

What it does, in order: discover the playable files, read their tags, normalize
and resolve conflicts by precedence, repair gaps from MusicBrainz (only with
`--musicbrainz`), write the artwork cache, build a scratch database, validate
it, and publish it by an atomic rename. Nothing is moved, renamed or deleted in
your music directory, ever.

Flags worth knowing:

| Flag | Effect |
| ---- | ------ |
| `--music-dir PATH` | override `paths.music_dir` for this run |
| `--library PATH` | override where the artifact is published |
| `--full` | ignore the previous build and rebuild from scratch |
| `--dry-run` | build and validate, publish nothing |
| `--musicbrainz` | fill gaps from MusicBrainz (off by default, rate-limited) |
| `--max-artwork N` | cap distinct images written (`0` skips the cache entirely) |
| `--json` | machine-readable report |

It prints a report and exits. The exit code is meant to be read by a cron
wrapper as well as by you:

| Code | Meaning |
| ---- | ------- |
| 0 | built and published (or built and validated, with `--dry-run`) |
| 1 | built, but validation refused publication — the previous library is intact |
| 2 | usage or configuration error; every problem is named at once |
| 3 | the build could not run: unreadable root, unwritable paths, nothing to build |

An incremental build (the default) re-reads only the files whose size,
timestamp or tags changed, so a rebuild after adding an album takes seconds
rather than the several minutes a first build takes. A `--full` build ignores
that history. Both produce the same library; the report says which one it was.

**Corrections the Builder makes and reports.** Trailing promo markers
(`-ADVANCE`, `[Clean_Version]`), tagging-tool prefixes on artist names
(`AlbumWrap - Kenny Chesney`), whitespace and control characters in tags, and
track numbers written as `05` or `5/12`. Every change is recorded per field,
with the file's original value beside it, so an artist that looks wrong on
telephone can be traced to the tag that said it. Ask with
`--json` and read `skip_details` and the provenance rows in the database.

**Files it cannot use are counted, not listed.** A library with 188 DRM
`.m4p` files reports `188 unsupported container: .m4p` on one line; the paths
are in the JSON. Anything it skipped while trying — damaged files, a directory
it could not read — is in the same section, because a build that lost an artist
in silence is the failure this report exists to prevent.

## Daily operation

There is not supposed to be any. The dashboard exists so that "is it broken or is
it the WiFi?" takes ten seconds to answer.

### Signing in

The appliance has one administrator and one password, and the password is not in
any file you edit.

On a box that has never been signed in to, `http://encore.local:8080/admin` shows a
form that asks for **one** field: the password you would like. Type it, twice is
not asked for, and you are in — that page is the same page that will later ask you
to sign in, because a separate setup route would be a page nobody tests twice
(ADR-013). Ten characters is the minimum; a shorter one is refused with a sentence
about the length rather than a rule you have to guess.

If the installer set `ENCORE_ADMIN_PASSWORD`, the credential already exists by the
time that page renders: the appliance's first boot reads the variable, hashes it
into `runtime.db`, and forgets it. Nothing is written on first sign-in — the value
lives in the database, and nothing reads the environment again afterwards. Leaving
the variable set is harmless and untidy, and `encore-install verify` will name it
as a stale line rather than complain about it, because a file in `/etc` that could
set this password would be a way to take over a machine by editing a line in it:
after the first boot, the row in `admin_state` wins, always (ADR-015).

A password too weak to store is reported in the journal and skipped rather than
fatal. The box still comes up, the first-run page is still there, and you find out
now rather than at the point where you needed the jukebox.

What a session is worth:

- **An hour by default** (`admin.session_minutes`), and the hour restarts each time
  you use it, so a queue-clear in the forty-ninth minute does not fail.
- **In memory only.** Restarting the appliance signs you out. That is deliberate: a
  token that survives a disk-image restore is a token somebody can restore into
  service (ADR-013).
- **One laptop is one session.** Five of you can sign in with the same password and
  each get a session; the dashboard says how many are open.
- **Five wrong passwords lock sign-in** for fifteen minutes (`FAILURE_WINDOW` in
  `encore/services/admin_service.py`), measured from the oldest of the five, on the appliance
  rather than per address — an appliance on a LAN has one address that matters and ADR-007
  forbids keeping notes about who was connecting. The sign-in page says roughly how long is left
  while you are looking at it, and a script gets a `429` with the exact seconds in `Retry-After`.
  A successful sign-in clears the count, so four mistypes do not get paid for later, and a locked
  appliance does not check the password at all — which is both the point of a limiter and the
  reason a lockout is not a hint about what you typed.
- **Changing the password ends every session, including yours.** You are sent back
  to the sign-in page with a line saying so. It is the only behaviour that makes
  "rotate it after the festival" mean something.
- **Signing out ends one session** — yours — and withdraws the cookie.

There is no CSRF token. The cookie is `HttpOnly` and `SameSite=Strict`, and a
request that would change something is refused if it arrives from a different
origin. The one thing that buys over a token is that there is no token to forget to
check, and the one thing it does not buy is protection against a *GET* — which is
why no administrative page changes anything when you load it.

### The screens

- **Dashboard** — Now Playing, what is waiting and how long until the last of it,
  the five transport buttons, a health line, and how many sessions are open. It
  redraws itself when the appliance says something happened; it does not poll.
- **Playback** — pause, resume, skip, stop, start. No seek: SAPRS 8.8's
  administrative list does not include it and a room that could jump into the
  middle of a track is a room that loses its place.
- **Queue** — every request, uncapped, with the two operations an operator has and
  a guest does not: take one out, take all the waiting ones out. Removing what is
  playing is a skip, not a silence. There is no reordering, and there is not going
  to be (SAPRS 8.1).
- **Health** — the aggregate, then each component's own words: `playback`,
  `library`, `search`, `runtime`. If mpv had to be restarted, that is where it
  says so.
- **Statistics** — plays, searches, requests, distinct songs heard, history rows,
  today and ever; a fortnight of trend as a table rather than a chart; the
  leaderboard. Peak concurrency and crossfade faults are v1.1 counters and are not
  here (SAPRS 10.3 marks them).
- **Logs** — the last few minutes of the appliance's own records, newest first,
  filterable by level. This is a ring buffer, not the journal: it holds 400 lines,
  it holds no tracebacks, and the page says `journalctl -u encore` beside the
  filter for the rest. Credentials are redacted here exactly as they are in the
  journal, by one rule rather than two.
- **Configuration** — the validated configuration, printed from the parsed model
  rather than from the YAML on disk, with anything that looks like a credential
  redacted. Read-only, and it says so: Encore will not edit YAML for you
  (SAPRS 12.3, 12.7).
- **Library** — where this library came from: version, schema and normalization
  rules, build date, the counts, whether FTS is available, and the artwork
  directory. It prints the Builder's own song tally *and* a live count, because a
  disagreement between those two is an install fault worth seeing rather than one
  number that might be a lie (SAPRS 6.10).
- **Password** — the change form, and the timestamp of the last successful sign-in,
  which is the only history this appliance keeps about the administrator.

A control answers with the part of the screen it changed. If your browser runs
JavaScript that is a redraw of the panel you pressed it in; if it does not, it is a
redirect back to the page. Both performed the command — the shape of the bug
`tests/regression/test_issue_28_administration_interface.py` exists to keep dead.

### What an operator does at a party

Fill the queue with requests, then press **Skip**. Or open `/admin/queue` and take
one request out. Every action on those screens is the same operation a guest's
button performs, run by you instead of by them, which is why the room's phone and
your laptop never disagree: the queue is one list in `runtime.db` and there is one
code path that changes it (ADR-011).

One asymmetry is worth knowing before somebody mentions it. **A pause reaches a
phone that asks and not a phone that is watching.** Playing, finishing, skipping and
advancing are facts the appliance announces; pausing is not one of them (ADR-004
decides what counts as a fact, and milestone 14 did not extend that list). A guest
looking at a paused track sees a progress bar that has stopped moving rather than a
"Paused" badge, until the next thing that happens redraws their screen. Skipping,
resuming and stopping are all announced.

## Maintenance

- **Rebuild the library**: run `encore-builder` (see
  [Building the library](#building-the-library)); it publishes by renaming a
  finished file into place, so a build that fails leaves the working library where
  it was (SAPRS 5.8). Then tell the running Server: **Library → Reopen library** on
  the dashboard's library page, or `POST /admin/library/reload` with your session
  cookie. There is no automatic detection and there is not going to be one — a
  Server that polled its own catalogue would be watching a filesystem for a change
  the Builder could simply have announced, and ADR-006's immutability is about
  writes rather than about refreshes.

  A reload cannot stop the party. If the new file is not a readable Encore library,
  the page refuses it and the appliance keeps playing the catalogue it had. If it
  is, every screen is told with a `LibraryReloaded` fact, and whatever is playing
  keeps playing — its path was resolved when it was queued.
- **Restart the appliance**: `systemctl restart encore` — the queue survives
  because it lives in `runtime.db`.
- **Upgrade Encore**: `sudo encore-install upgrade`. It stops the service, replaces
  the code, re-resolves the dependencies, copies `runtime.db` to
  `runtime.db.pre-upgrade`, and starts again — the migrations are forward-only and
  that copy is the way back, which the appliance will not take for you. Your
  configuration is not touched, and if it disagrees with a flag you passed, the run
  says so by name (SAPRS 13.8, 13.9).
- **Check an install**: `sudo encore-install verify` prints 13.11's acceptance list
  with a fix under every line that is not green. It changes nothing and is safe to
  run during a party.
- **Uninstall**: `sudo encore-install remove` deletes the application and keeps
  everything hard to reproduce — the configuration, the databases, the artwork and
  your music. `--purge-config` and `--purge-data` are separate flags because they
  are separate acts, and both need `--yes`. The service account is left behind: a
  stray account is a security review finding, and deleting one you did not create
  is not an installer's business.
- **Lost the password**: `sudo encore-install reset-credential` stops the service,
  clears the credential row, and starts it again. The first-run page comes back.
  The queue, the history and the music are untouched (ADR-013).
- **mpv dies**: the Supervisor restarts it and publishes `PlaybackRecovered`; if
  the recovery counter keeps climbing, check `Troubleshooting.md`.
- **Disk**: artwork cache and `runtime.db` grow; both are safe to compact while
  the server is stopped.
- **Backups**: `library.db` (rebuildable), `runtime.db` (history), and
  `/etc/encore/config.yaml` (small, precious). `/etc/encore/environment` belongs in
  the same copy: it holds the credential you seeded, at mode `0600`, and a restored
  appliance with no environment file is an appliance whose operator is locked out of
  it by their own backup.

## Security posture

- Guests stay anonymous; the administrator's password is the only credential on the
  box, and there is exactly one of them. No guest accounts, no per-device tokens,
  no "remember me" (ADR-007).
- The password is stored hashed (Argon2id, or bcrypt if the platform's Argon2
  library is missing) in `runtime.db`. It is never in the image, never in a
  template, never in a log, and `runtime.db`'s bytes do not contain it — which is
  asserted rather than believed.
- The `admin:` section of `encore.yaml` holds one number and cannot hold a
  password: an unknown key there is a startup error rather than a false sense of
  security (SAPRS 12.6, AEP 17).
- Sessions are random 256-bit tokens, held in memory, carried in a cookie that
  `HttpOnly` and `SameSite=Strict` describe. A restart ends them.
- Bind to the LAN; do not expose the appliance through a tunnel without reading
  ADR-007 first. The cookie cannot be `Secure` on a plain-HTTP appliance, which is
  the trade ADR-013 records rather than hides: on the LAN the exposure to defend is
  a cross-site request from a device that is already inside, and `Strict` plus the
  origin check is what answers it.
- Failed sign-ins are counted per appliance, never per address, and nothing about
  who was connecting is kept. The one timestamp recorded is the last successful
  sign-in.
- Report vulnerabilities privately — see [`SECURITY.md`](../SECURITY.md).

## Known gaps during bootstrap

Milestones 1–15 work: configuration, the domain model, the event bus, both
databases with their repositories, the Library Builder, search, playback, the
queue, the HTTP runtime, the guest interface, the live stream, the administrative
interface, and the installer — which is where the "one command from a clean SD
card" claim now lives, along with `GET /qr` and the `ENCORE_ADMIN_PASSWORD`
seeding this page has always described. Not yet implemented: the full-scale Party
Simulation driver (16).

The lists above are what exists; where a commitment is still open it says so, so
that documentation and code land together (AEP 11).

The Builder is usable now and is the one part of the appliance you can run
against real music today; without a Server it simply has nothing to publish to.
