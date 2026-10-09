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

## Where the code is

| Package | State | Notes |
| ------- | ----- | ----- |
| `encore/domain/` | implemented | SAPRS Ch. 4 entities, enums, transition table; frozen dataclasses, typed ids |
| `encore/events/` | implemented | the eight AIG 8 facts + `EventBus`; `EVENT_VOCABULARY` is the set as data |
| `encore/config/` | implemented | Pydantic models for `examples/config.yaml` + `ConfigurationService` |
| `encore/services/` | partial | `LoggingService` and `build_core_services()`; the other services arrive with their milestones |
| `encore/utilities/` | partial | `clock.py` (injectable time), `redaction.py` (log safety) |
| `encore/repositories/` | placeholder | step 5 |
| `encore/search/`, `encore/playback/` | placeholder | steps 7-8 |
| `encore/api/`, `encore/controllers/`, `templates/`, `static/` | placeholder | steps 11-14 |
| `apps/builder/`, `apps/server/` | placeholder | entry points land with steps 6 and 11 |

## Runtime services (SAPRS 2.4, AIG 7)

ConfigurationService · LibraryService · SearchService · QueueService ·
PlaybackService · PlaybackSupervisor · HealthService · StatisticsService ·
EventBus · SSEPublisher · LoggingService. One capability each, dependencies
injected by constructor, cooperating through the Event Bus.

`build_core_services()` in `encore/services/container.py` is the composition
root: the only place that constructs the core, returning a frozen `CoreServices`.
It reads no environment variable, holds no module-level instance, and configures
logging as its one process-wide effect — undone by `logging.remove()`.

## Events (AIG 8, ADR-004)

`SongQueued`, `SongStarted`, `SongFinished`, `QueueAdvanced`,
`PlaybackRecovered`, `LibraryReloaded`, `BuildCompleted`, `HealthChanged`.
Immutable, typed, timestamped, independent of HTTP. Handler failure is isolated.

`publish()` never blocks a caller on a handler: synchronous handlers run inline
in registration order and coroutines are scheduled, so a slow subscriber cannot
delay playback (SAPRS 11.4). `publish_async()` and `drain()` are for shutdown and
for tests, where ordering must be observed.

## Storage (SAPRS 5, ADR-006, ADR-009, ADR-010)

- `library.db`: normalized `artists`, `albums`, `songs`, `artwork`,
  `music_files` + denormalized FTS5 search structures; published atomically;
  opened read-only by the runtime.
- `runtime.db`: queue, playback history, statistics, administrative state.
- Artwork stored as files with stable database references.
- Access is split by mutability (ADR-009): raw `sqlite3` with a read-only URI for
  `library.db`, SQLAlchemy 2.x transactions for `runtime.db`. Neither an ORM nor a
  `Session` crosses a repository boundary; repositories return domain types.
- Opening a database file is not the same as having one: both access layers *create*
  a missing file and hand back a working empty library. Startup must assert the
  library's shape — file exists, expected tables present, song count logged — not
  merely that a connection succeeded (ADR-009).
- `library.db` schema is written only by the Builder and versioned inside the file;
  `runtime.db` is migrated by numbered, forward-only modules.

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

## Configuration (SAPRS 12, ADR-008, ADR-010)

Installation-time YAML, validated at startup, never rewritten by the app, never
used as a database. Admin UI shows it read-only. Secrets from the environment or
system store.

`paths.music_dir` (default `/opt/music`, Builder-only) is specified by SAPRS 12.2's
"Library location" and **not yet implemented**: `PathsConfig` has no such field.
It must be added to the model, `examples/config.yaml` and the Administrator guide in
one change, because `extra="forbid"` plus the test that executes the example makes
any subset of the three fail CI.

## Deployment (SAPRS 13)

Raspberry Pi 4 / Raspberry Pi OS 64-bit primary; Debian 12 and Ubuntu 24.04
secondary. systemd units, `/opt/encore`, `/etc/encore`, `/var/lib/encore`,
`/var/cache/encore`. User music is read from `/opt/music`; the `encore` service
account needs read access to it (SAPRS 13.5) while owning nothing there. Manual
installation is authoritative; `encore-install` automates the same steps.

## Non-goals for v1 (SAPRS 1.7)

Ratings, favourites, smart playlists, remote displays, recommendations,
AI discovery, cloud-dependent playback.
