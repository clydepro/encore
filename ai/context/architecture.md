# Context Bundle — Architecture

A compact, citable summary for AI sessions. It is a pointer, not a replacement:
the SAPRS is authoritative (AEP 2).

## One paragraph

Encore is a headless LAN jukebox for a Raspberry Pi 4: an offline **Library
Builder** produces an immutable SQLite library, and a long-running **Server**
serves a mobile-first HTMX interface plus a versioned JSON API and SSE, playing
audio through mpv with strict FIFO queueing and anonymous guests.

## Two applications (SAPRS 2.1, ADR-001)

| Aspect | Builder | Server |
| ------ | ------- | ------ |
| Lives in | `apps/builder/` | `apps/server/` |
| Runs | offline, then exits | continuously |
| Writes | `library.db`, artwork cache, reports | `runtime.db` only |
| Reads | music files, MusicBrainz | `library.db` read-only |
| Must never | import playback | write `library.db` |

## Layers (SAPRS 15.2)

```text
Presentation → Application → Domain → Repositories → Infrastructure
```

Imports point inward only. HTTP concepts stop at `encore/controllers/` and
`encore/api/`; SQL stops at `encore/repositories/`.

## Runtime services (SAPRS 2.4, AIG 7)

ConfigurationService · LibraryService · SearchService · QueueService ·
PlaybackService · PlaybackSupervisor · HealthService · StatisticsService ·
EventBus · SSEPublisher · LoggingService. One capability each, dependencies
injected by constructor, cooperating through the Event Bus.

## Events (AIG 8, ADR-004)

`SongQueued`, `SongStarted`, `SongFinished`, `QueueAdvanced`,
`PlaybackRecovered`, `LibraryReloaded`, `BuildCompleted`, `HealthChanged`.
Immutable, typed, timestamped, independent of HTTP. Handler failure is isolated.

## Storage (SAPRS 5, ADR-003, ADR-006)

- `library.db`: normalized `artists`, `albums`, `songs`, `artwork`,
  `music_files` + denormalized FTS5 search structures; published atomically;
  opened read-only by the runtime.
- `runtime.db`: queue, playback history, statistics, administrative state.
- Artwork stored as files with stable database references.

## Playback (SAPRS 7, ADR-005)

mpv over JSON IPC, owned solely by `encore/playback/`. State machine
`Idle → Loading → Playing (→ Paused) → Finished → Idle`, with
`Error → Recovering → Idle/Playing`. Supervisor launches, monitors, detects
crashes, restarts, reconnects and reports health. Gapless where media permits;
crossfade configurable; progress ~1 Hz.

## Queue (SAPRS 8, AIG 12)

Strict FIFO. Duplicates allowed. Guests anonymous. Idle ⇒ play immediately;
otherwise append. No priority, no reordering.

## Interface (SAPRS 9, ADR-002)

Jinja2 + Tailwind + HTMX fragments in a persistent shell; SSE for live state.
No SPA framework, no client-side routing, no polling.

## Configuration (SAPRS 12, ADR-008)

Installation-time YAML, validated at startup, never rewritten by the app, never
used as a database. Admin UI shows it read-only. Secrets from the environment or
system store.

## Deployment (SAPRS 13)

Raspberry Pi 4 / Raspberry Pi OS 64-bit primary; Debian 12 and Ubuntu 24.04
secondary. systemd units, `/opt/encore`, `/etc/encore`, `/var/lib/encore`,
`/var/cache/encore`. Manual installation is authoritative; `encore-install`
automates the same steps.

## Non-goals for v1 (SAPRS 1.7)

Ratings, favourites, smart playlists, remote displays, recommendations,
AI discovery, cloud-dependent playback.
