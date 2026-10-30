# Troubleshooting

Status: **implemented, install to playback** — expanded by every milestone that
can fail, and by every issue closed with a regression test. Organised
symptom-first, because that is how someone searches it at a party.

## Before anything else

1. Admin dashboard → Health. Green components mean the box is fine and the
   question is about the network or the device.
2. `systemctl status encore` and `journalctl -u encore -n 200`.
3. `mpv --version` and whether audio works at all outside Encore
   (`speaker-test`, `wpctl status`).
4. Restart is allowed and cheap: `systemctl restart encore`. The queue survives.

## Symptoms

### Guests cannot connect

- Wrong network or a guest VLAN with client isolation → join the LAN the box is
  on, or route mDNS/DNS-SD.
- `jukebox.home.arpa` does not resolve → `avahi-daemon` running? Try the address
  from `hostname -I`.
- Firewall → port from `server.port` in `/etc/encore/config.yaml` must accept
  LAN traffic.
- QR code points at an old address → regenerate from the admin dashboard.

### No sound

- mpv not running: Health shows the supervisor state; restart the service and
  watch the log for `PlaybackRecovered`.
- Wrong output/device: `audio.output`, `audio.device` are installation
  configuration (SAPRS 7.7) — hardware assumptions never live in code. This is
  the common one, and it has its own section below because the log line lies
  about it.
- Output volume/ducking by the OS, or the ALSA device is held by another
  process.
- Health green, log full of refusals: read
  [mpv refuses every file](#mpv-refuses-every-file-audio-output-initialization-failed).

### mpv refuses every file: "audio output initialization failed"

```text
{"level":"WARNING","logger":"encore.playback","message":"mpv refused the file",
 "song_id":900,"file":"/opt/music/.../04_Bad_Moon_Rising.m4a",
 "detail":"audio output initialization failed"}
```

The file is not the problem. Nothing in the library is. mpv opened the file,
could not open a sound device, and gave up — which is why mp3 and m4a fail
identically, why a rebuild changes nothing, and why the `song_id` changes every
time while the `detail` never does. Issue
[#48](https://github.com/clydepro/encore/issues/48) was a room with no music for
an hour, on a Pi 4 whose `audio.device` was `default`.

On a Raspberry Pi, `default` is whichever card registered first, and that is
usually HDMI:

```console
$ cat /proc/asound/cards
 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0        ← what `default` opens
 2 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones   ← what the speakers are
```

Ask mpv, as the account the service runs under, which is the part that surprises
people — a sound server in someone's desktop session is not reachable by
`User=encore`:

```console
$ sudo -u encore mpv --ao=alsa --audio-device=help | head   # the names worth using
$ sudo -u encore mpv -v --ao=alsa --audio-device=default somefile.mp3
[ao/alsa] opening device 'default'
[ao/alsa] Playback open error: Unknown error 524        ← card 0, nothing plugged in
[cplayer] finished playback, audio output initialization failed (reason 4)
$ sudo -u encore mpv -v --ao=alsa --audio-device=alsa/plughw:CARD=Headphones,DEV=0 somefile.mp3
[ao/alsa] opening device 'plughw:CARD=Headphones,DEV=0'
[cplayer] finished playback, success (reason 0)         ← this one is the fix
```

Then set it in `/etc/encore/config.yaml` and restart — the queue survives:

```yaml
audio:
  output: alsa
  device: alsa/plughw:CARD=Headphones,DEV=0
```

What to check in that order, and what each answer means:

| Check | If it is wrong |
| ----- | -------------- |
| `Admin → Health` says `playback: degraded` with mpv's sentence in it | You are on a build before #48, where every file got its own WARNING and health stayed green. Upgrade, or read the log's `detail` field |
| `id encore` includes `audio` | `sudo usermod -aG audio encore`, then restart (SAPRS 13.5) |
| `output: pipewire` while PipeWire runs in a login session | Use `alsa`. A system service has no session bus and no `/run/user/<uid>`, so it cannot reach a per-user sound server |
| The device names a card with no sink behind it | Name the card you can hear. `plughw:` converts rates and channels in software, which is what you want from a fixed appliance |
| A device name mpv cannot parse | Nothing rejects it: mpv logs `Trying the default audio device...` and fails exactly as a dead card fails, so a typo and a broken output look identical. Copy the string from `mpv --ao=<output> --audio-device=help`, which prints what mpv accepts rather than what ALSA calls the hardware |
| The mixer, not the device | `audio.volume` is mpv's software preamp; an ALSA control at 0 % is silence whatever mpv opens. Check `amixer -c <N> get Master` and, on a Pi, `get Headphone` — they are different controls |
| Another process holds the card (`aplay` in a timer, a desktop PipeWire that woke up) | `fuser /dev/snd/pcmC*D0p`. An appliance should own its output; if a desktop session shares the box, that session is the thing to stop |

The one that is easy to miss: **`alsa/hw:2,0` and `alsa/plughw:CARD=Headphones,DEV=0` will
fight PipeWire for the card.** On a headless appliance that is exactly what you want.
On a box that also has a desktop, run the appliance through a system-wide PipeWire
instead, or the two will take turns failing to open the device.

### Search is slow or empty

- Library not published, or the server is pointing at a stale `library.db`
  path → check `paths.library_db` and the Library page version.
- FTS5 missing from the linked SQLite build — check `sqlite3.sqlite_version`
  from Python, or run the FTS5 probe in `tests/support/sqlite.py`.
- Library build skipped indexing — see the Builder validation report.

### Queue oddities

- Songs appear twice: allowed by design (SAPRS 8) — duplicates are the rule.
- A song never plays: file missing or unreadable; check the build report and the
  mount, and look for `SongFinished` with an error path in the log.
- Nothing advances after an mpv crash: recovery events and the supervisor
  backoff list (`playback.restart_backoff_seconds`) tell you which stage is
  stuck.

### Now Playing stops updating

- SSE connection dropped when the phone slept — reconnect is automatic; if the
  browser tab was backgrounded longer than the proxy timeout, reload once.
- A reverse proxy buffering responses breaks streaming; disable buffering for
  `/events`.
- An event subscriber crashed: the bus isolates it (SAPRS 11.9), so playback
  keeps running while one client's view goes stale.

### The installer failed

Start with the exit code; it says whose fault it is.

| Code | Meaning | Do this |
| ---- | ------- | ------- |
| `0` | Worked, including "there was nothing to do" | Nothing |
| `2` | The command line is wrong | Read the sentence. `--purge-config` with `install` is this code, not a warning |
| `3` | The machine is not ready | Fix the machine, run it again. Nothing was changed |
| `4` | The machine is fine; something about Encore is not | The check that failed is printed above a fix line |

A `3` is the one that surprises people, and each of its five causes is named by a
check rather than by a traceback:

- **Not root, and not staging.** The installer never needs to be root to be
  understood: `--root /tmp/rehearsal --dry-run` shows the whole plan from an
  ordinary account. Run it that way first if you are wondering what it would do.
- **No systemd.** A container has no PID 1 to register with, and neither does
  WSL1. That is a `--no-service` install and you start the appliance by hand; it is
  not a skipped step, and the report says which. Note that this is a question about
  the *machine*: an appliance that has no `encore.service` yet is a machine waiting
  for this command, not a prerequisite failure — see
  [the installer says the service manager is missing, and it is there](#the-installer-says-the-service-manager-is-missing-and-it-is-there)
  if that is what you are reading.
- **The port is taken.** `verify` names the port and the address. `--port` on the
  command line changes what gets written; a `config.yaml` that already exists wins
  and the run will tell you the setting it did not apply.
- **`mpv` or `uv` is missing.** Usually. Sometimes what is missing is the
  directory they are in — see
  [the installer says uv is missing, and it isn't](#the-installer-says-uv-is-missing-and-it-isnt),
  which is the first question to ask because it costs one line instead of a
  package install. `mpv` is not optional: an appliance that cannot play is a web
  server with a music directory. `apt install mpv` and re-run; the installer is
  idempotent, so re-running is the recovery.
- **The music directory is not there.** A warning, not a failure. The Server never
  scans a directory (ADR-010); only the Builder reads one, so an appliance with no
  mounted library is correctly installed and empty.

An install that stopped halfway is the case the whole design is built for. Re-run
`install`: it observes the machine, plans only what is missing, and says so. If
somehow you would rather see that plan first, `--dry-run` prints it and changes
nothing.

### The installer says the service manager is missing, and it is there

```text
FAIL  service manager: inactive
       → Install with --no-service and start the server by hand (SAPRS 13.2).
```

On a machine whose PID 1 is systemd, that line means the installer found no
`encore.service` — which is true, and is what you were about to ask it to create.
Issue #43 was this sentence: the check decided the *machine* was unusable from an
answer about the *unit*, on the first install, on the platform the specification
names as primary. Fixed; if you are on a release that still prints it, ignore it
and run the command without `--no-service`, or upgrade the installer. Do not work
around it with `--no-service`, because that is the option that leaves an appliance
that never comes back after a power cut.

The same report used to open with `nothing to do — the machine already matches this
request`, four lines above `a prerequisite is unmet`. Both sentences describe an
empty plan, and an empty plan meant three different things: nothing to do (13.7's
idempotence, which is a result), this command asks questions (`verify`), or nothing
was planned because the machine failed a check first. The report now distinguishes
them, in text and in `--json` as `"planning": "planned" | "not-asked" | "blocked"`.

### A rehearsal did not register the service

`--root` installs into a tree, and `systemctl` cannot be pointed at a tree — it
asks PID 1 about `encore.service` and finds whatever the host has. So a rehearsal
writes the unit file and asks nothing of the machine (#44), and says so:

```text
The host's service manager was not asked to do anything: on the appliance,
systemctl daemon-reload && systemctl enable --now encore.
```

That is the rehearsal working, not a step that failed. It is the same rule that
already stops `--root` from creating a service account: a command that changes a
prefix must not change the machine standing under it — including `upgrade --root`,
which used to stop a running appliance before copying files into a directory it does
not own.

### The installer says uv is missing, and it isn't

`uv` installed with the astral script goes into `~/.local/bin`. The install
command runs under `sudo`, and `sudo` replaces `PATH` with its `secure_path` —
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` — which contains
no home directory at all. A correctly installed `uv` is therefore invisible to a
correctly invoked installer. This is not a rare configuration; it is what the two
sets of instructions in this repository say to do, in that order (#38).

The installer searches your `PATH`, then `~/.local/bin`, `~/bin` and
`~/.cargo/bin` for both your own home and the home of `SUDO_USER` — the account
that typed `sudo`. A `--root` rehearsal that still cannot find it is a machine
where `uv` is genuinely absent:

```bash
sudo encore-install install --root /tmp/rehearsal --no-service --dry-run \
  | grep binaries
#   ok    binaries: mpv at /usr/bin/mpv, uv at /home/you/.local/bin/uv
```

Every failure line prints the directories it searched, so the answer to "is this
machine broken or is my PATH short" is in the report rather than in an hour of
`echo $PATH`. If you need to name the location yourself:

```bash
sudo PATH="$PATH" ./scripts/encore-install install     # pass your PATH through
```

That last form is a workaround, not the intended path; if it is the only thing
that works on your machine, the search list above is missing a directory and that
is worth an issue.

### An install with `--root` leaves ownership unset

A line like this in a rehearsal's report is not a failure, and the install exits 0:

```text
ownership not set (no 'encore' account in a staged install)
```

`--root` rehearses an install on a machine it must not change, so the plan skips
`useradd` (SAPRS 13.7). The ownership step then has no account to name, and
reporting that as a machine fault would make the rehearsal fail for describing
itself (#41). On a real appliance the account is created in the same run and the
`chown` succeeds; if it *fails* there, that is the serious case — the service
cannot read its own configuration — and the install stops rather than continuing.

### The configuration was not applied

Not an error, and the most common reason an appliance seems to ignore you. An
existing `/etc/encore/config.yaml` always wins over a flag (SAPRS 13.8) — the
installer keeps your file and names the settings that disagreed:

```text
keep /etc/encore/config.yaml as it is (SAPRS 13.8)
  → server.port is 9100 there, 8099 asked for. Edit the file if you meant those changes.
```

If the file is *invalid* the run fails instead of quietly proceeding, and it says
every offending key at once (12.5). It still will not overwrite the file, because
replacing something you cannot read destroys the evidence and your intent at the
same moment.

### The service will not start

- Configuration is validated at startup and refuses to run half-configured
  (SAPRS 12.5) — the message names the offending key.
- `library.db` unreadable or from an incompatible build → health reports it, the
  server does not guess.
- Port already in use, or `/var/lib/encore` permissions for the `encore` user.
  `sudo encore-install verify` checks both, and every other item on SAPRS 13.11's
  acceptance list, with a fix under each line that is not green.
- `Failed at step EXEC … Permission denied`, `status=203/EXEC`: the interpreter, not
  the program. See
  [the service fails at exec with Permission denied](#the-service-fails-at-exec-with-permission-denied).
  Nothing in the acceptance list used to be able to say this, so an install could be
  green and the appliance dead (#46).
- If the sign-in page says no administrator has signed in here yet when you are
  sure `ENCORE_ADMIN_PASSWORD` was set: the value is read on the *first* boot only,
  and never overwrites a credential that already exists (ADR-015). A too-weak value
  is skipped with a line in the journal rather than being fatal. `encore-install
  reset-credential` puts the first-run page back.

### The service fails at exec with Permission denied

```text
encore.service: Failed to execute /opt/encore/venv/bin/encore-server: Permission denied
encore.service: Failed at step EXEC spawning /opt/encore/venv/bin/encore-server: Permission denied
encore.service: Main process exited, code=exited, status=203/EXEC
```

`203/EXEC` with *Permission denied* is not a mode bit on that file, and `ls -l` will
mislead you: the script is `0755` and correct. A script is an instruction about which
program to run, so follow it instead —

```bash
namei -l "$(readlink -f /opt/encore/venv/bin/python)"
sudo -u encore /opt/encore/venv/bin/python -c 'print("reachable")'
```

— and if the walk stops at somebody's home directory, that is #46. Debian 12 ships
Python 3.11 and Encore needs 3.12, so `uv` downloads one, and where it puts the download
is the home of whoever ran the command: under `sudo`, inside `/root`, which is
`drwx------`. The venv is then a chain of symlinks into a directory `User=encore` cannot
search. Nothing the acceptance list could ask about a file's mode would notice, which is
why an install of that release printed `ok environment` over a service that could not
start.

The fix is one command, and it is safe to re-run: `sudo encore-install install` now puts
the interpreter in `/opt/encore/python` and builds the environment against it explicitly,
and it treats "the service account cannot reach the interpreter" as drift — so on a
machine already installed by the older release it repairs rather than reporting that
nothing needs doing. If you would rather not run the installer, move the interpreter it
already paid for:

```bash
sudo mkdir -p /opt/encore/python
sudo cp -a /root/.local/share/uv/python/. /opt/encore/python/
sudo chmod -R a+rX /opt/encore/python
sudo env UV_PYTHON_INSTALL_DIR=/opt/encore/python \
     uv sync --frozen --no-dev --project /opt/encore/application \
     --python "$(ls -d /opt/encore/python/cpython-3.12.*/bin/python3.12 | tail -1)"
sudo systemctl restart encore
```

The same arithmetic applies to a venv you built yourself. Do not run `uv sync` as root and
expect the appliance to agree: either name the interpreter (`--python`), or run the
command as the service account — though that puts a 60 MB interpreter under
`/var/lib/encore`, which `remove --purge-data` then owns.

### Library build problems

- Metadata repair needs MusicBrainz; the Builder is the only component allowed
  to reach the network (ADR-001).
- A build that fails validation leaves the previous library active (SAPRS 5.8) —
  read the validation report rather than republishing blindly.
- A build that ends in a traceback, not a report, published nothing: the library
  the Server is using is still the previous one.
- Covers gone after a rebuild that printed no error: that was
  [#52](https://github.com/clydepro/encore/issues/52), and an incremental build now
  carries the references it made before. A library that is *already* missing them
  stays missing them until a `--full` build re-reads the files, because an
  incremental one reuses what the last artifact said.
- Every cover is a grey tile: read
  [the artwork cache is full and the library has no covers](#the-artwork-cache-is-full-and-the-library-has-no-covers).

### The artwork cache is full and the library has no covers

```console
$ sudo sqlite3 /var/lib/encore/library.db \
    'select (select count(*) from artwork), (select count(*) from songs where artwork_id is not null)'
0|0
$ find /var/lib/encore/artwork -name '*.jpg' | wc -l
222
```

That pair of numbers is a specific story: the Builder wrote the images and then
failed before it wrote any references, so the library in place is one the operator
managed to publish with the pictures switched off. The build log says so:

```text
File "…/apps/builder/construction.py", line 166, in _write_artwork
sqlite3.IntegrityError: UNIQUE constraint failed: artwork.relative_path
```

Issue [#51](https://github.com/clydepro/encore/issues/51) was this, on a library
of 3,049 files: one album's cover, tagged twice, exists as two byte strings that
decode to identical pixels. Normalization re-encodes, so both forms write one
cache file — and the artwork stage deduplicated on the bytes it *found* rather than
the bytes it *wrote*, which handed the database two references naming one path. A
library whose `artwork` table is empty above a cache that is not is a library from
before that fix, whatever the cache looks like.

Its sibling, [#52](https://github.com/clydepro/encore/issues/52), produced the same
appliance symptom by a different route: an *incremental* build reuses the tags of
every file that has not changed, a reused file arrives at the artwork stage with no
image bytes (SAPRS 5.6 keeps them out of the database, and a JSON cache cannot hold
them either), and a stage that sees no image writes no reference. So one ordinary
rebuild published 0 rows over 222, validated, and renamed it into place. That is
fixed: the references now carry across the same incremental key the tags do. What
the fix cannot do is heal a library those bugs already published — an incremental
run reuses references *from the previous artifact*, and that one has none to give.

To repair an appliance that was built that way, rebuild with the current code and
then tell the running Server — no restart, and the queue is untouched:

```console
$ sudo encore-builder --full
  Artwork            0  images written to the cache (222 in this library)
  Published /var/lib/encore/library.db
```

`--full` is not decoration, and the two numbers on that line are the diagnosis: a
healthy incremental build reads `0 written (222 in this library)`, and a build that
reports zero in *both* brackets has nothing to show a guest. Then **Library → Reopen
library** on the dashboard's library page (see
[Maintenance](Administrator-Guide.md#maintenance) — the endpoint behind it wants
your session cookie, so `curl` without one is not the way to ask).

The other number to check is `select count(*) from songs where artwork_id is not
null`, which should be within a few percent of how many files carry a picture.

## Diagnostics worth collecting

- Admin → Logs export (structured JSON).
- `journalctl -u encore --since "10 min ago"`.
- Health page output and the library build report.
- `uv pip list` / `uv lock --check` output for "works here, not there".
- Exact symptom, device and browser for UI problems.

## Getting help

Open a [Question](https://github.com/clydepro/encore/issues/new/choose) or
[Bug report](https://github.com/clydepro/encore/issues/new/choose) issue with
the details above. Regressions get fixed with a test in `tests/regression/`, so
a reliable reproduction is the most valuable thing you can attach.
