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
4. Publish `library.db` + artwork built by the Builder.
5. Install and enable the systemd units; verify the `jukebox` mDNS name.
6. Open the admin dashboard, confirm health is green, print the QR code.

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

- **Rebuild the library**: run the Builder to a new file, validate, publish
  atomically, then `POST /admin/library/reload`. A failed build never replaces a
  working library (SAPRS 5.8).
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

Milestones 1–2 work only: no HTTP interface, no mpv control, no admin login. The
lists above are commitments for the milestones named in AIG Chapter 21, so that
documentation and code land together (AEP 11).
