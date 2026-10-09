# Administrator Guide

Status: **outline** — the administrative interface lands with milestone 14
(AIG 21). Written for the person who installs the box, and who will be asked
about it at a party.

## Mental model

Two applications, two databases, one appliance:

```text
music files → Library Builder → library.db (immutable) → Encore Server → mpv → speakers
                                            runtime.db (queue, history, stats) ↗
```

You build the library elsewhere, publish the database, then run the server and
forget about it (ADR-001, ADR-006, ADR-008).

## Installation

Authoritative steps are the manual ones (SAPRS 13.2); `encore-install` automates
the same sequence. Target: Raspberry Pi 4 on Raspberry Pi OS 64-bit, Debian 12
or Ubuntu 24.04.

Outline:

1. Create the `encore` system user and the filesystem layout of SAPRS 13.3
   (`/opt/encore`, `/etc/encore`, `/var/lib/encore`, `/var/cache/encore`).
2. `uv sync --frozen` into `/opt/encore/venv`.
3. Write `/etc/encore/config.yaml` from
   [`examples/config.yaml`](../examples/config.yaml).
4. Publish `library.db` + artwork built by the Builder — see
   [Building the library](#building-the-library). The Installer does not do this
   for you: it needs a music directory that only you know the location of.
5. Install and enable the systemd units; verify the `jukebox` mDNS name.
6. Open the admin dashboard, confirm health is green, print the QR code.

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

There is not supposed to be any. The dashboard exists so that "is it broken or
is it the WiFi?" takes ten seconds to answer.

- **Dashboard** — Now Playing, queue depth, health, uptime.
- **Playback** — pause, resume, skip, stop, seek. Nothing here changes queue
  rules.
- **Queue** — inspect and remove entries. Reordering is intentionally absent.
- **Health** — per-component status, mpv process state, database accessibility,
  last recovery.
- **Statistics** — plays, most-requested, peak concurrency.
- **Logs** — structured, filterable, no secrets (SAPRS 16 logging rules in AEP).
- **Configuration** — read-only summary. Encore will not edit YAML for you
  (SAPRS 12.3, 12.7).
- **Library** — version, song/artist/album counts, build date, validation report
  reference.

## Maintenance

- **Rebuild the library**: run `encore-builder` (see
  [Building the library](#building-the-library)), then
  `POST /admin/library/reload`. A failed build never replaces a working
  library (SAPRS 5.8), and a successful one leaves the previous file's inode
  intact for any Server process still holding it open.
- **Restart the appliance**: `systemctl restart encore` — the queue survives
  because it lives in `runtime.db`.
- **mpv dies**: the Supervisor restarts it and publishes `PlaybackRecovered`; if
  the recovery counter keeps climbing, check `Troubleshooting.md`.
- **Disk**: artwork cache and `runtime.db` grow; both are safe to compact while
  the server is stopped.
- **Backups**: `library.db` (rebuildable), `runtime.db` (history), and
  `/etc/encore/config.yaml` (small, precious).

## Security posture

- Guests stay anonymous; the admin session is the only credential on the box.
- Bind to the LAN; do not expose the appliance through a tunnel without reading
  ADR-007 first.
- Session secret comes from the environment or system store (SAPRS 12.6), never
  from config files or source.
- Report vulnerabilities privately — see [`SECURITY.md`](../SECURITY.md).

## Known gaps during bootstrap

Milestones 1–5 work: configuration, the domain model, the event bus, both
databases with their repositories, and the Library Builder. Not yet implemented:
the HTTP interface, mpv control, the admin login, and the installer. The lists
above are commitments for the milestones named in AIG Chapter 21, so that
documentation and code land together (AEP 11).

The Builder is usable now and is the one part of the appliance you can run
against real music today; without a Server it simply has nothing to publish to.
