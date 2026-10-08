# Troubleshooting

Status: **outline** — expanded by every milestone that can fail, and by every
issue closed with a regression test. Organised symptom-first, because that is
how someone searches it at a party.

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
  configuration (SAPRS 7.7) — hardware assumptions never live in code.
- Output volume/ducking by the OS, or the ALSA device is held by another
  process.

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

### The service will not start

- Configuration is validated at startup and refuses to run half-configured
  (SAPRS 12.5) — the message names the offending key.
- `library.db` unreadable or from an incompatible build → health reports it, the
  server does not guess.
- Port already in use, or `/var/lib/encore` permissions for the `encore` user.

### Library build problems

- Metadata repair needs MusicBrainz; the Builder is the only component allowed
  to reach the network (ADR-001).
- A build that fails validation leaves the previous library active (SAPRS 5.8) —
  read the validation report rather than republishing blindly.

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
