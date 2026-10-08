# Encore
## Software Architecture & Product Requirements Specification (SAPRS)

**Project:** Encore  
**Document:** Software Architecture & Product Requirements Specification  
**Version:** 1.0  
**Status:** Implementation Baseline  
**Audience:** Maintainers, developers, AI coding agents, reviewers, contributors

---

## Document Purpose

This document is the authoritative architectural and product specification for Encore.

The document defines the product vision, requirements, architecture, data model, runtime behavior, interfaces, deployment model, testing strategy, engineering standards, and roadmap.

The **AI Implementation Guide (AIG)** translates this specification into implementation-oriented instructions. The **AI Engineering Playbook (AEP)** defines how AI-assisted development is to be performed. Architectural Decision Records (ADRs) capture significant decisions and their rationale.

Where an implementation conflicts with this specification, the implementation must not silently redefine the requirement. The conflict must be identified and resolved explicitly.

---

# Table of Contents

1. Product Vision & Requirements
2. System Architecture
3. Technology Stack & Development Environment
4. Domain Model
5. Database Architecture & Storage Model
6. Library Builder
7. Playback Service
8. Queue & Playback Orchestration
9. Web User Interface
10. HTTP API & Real-Time Communication
11. System Architecture & Service Composition
12. Configuration & Runtime State
13. Deployment & Installation
14. Testing Strategy & Quality Assurance
15. Development Standards & Project Structure
16. Roadmap & Project Manifesto

---

# Chapter 1 — Product Vision & Requirements

## 1.1 Purpose

Encore is a headless music jukebox designed for local-network parties.

Guests use a mobile-friendly web interface to browse and search a pre-built personal music library, select songs, and place them into a FIFO playback queue.

The jukebox is intended to behave like an appliance rather than a conventional web application.

The music, not the software, should be the center of attention.

## 1.2 Primary Goals

Encore shall:

- Run reliably on a Raspberry Pi 4.
- Support a music library of approximately 15,000 songs.
- Support MP3, FLAC, AAC, and M4A.
- Provide anonymous guest access.
- Provide mobile-first browsing and search.
- Support artist, album, and song discovery.
- Provide strict FIFO queueing.
- Allow duplicate songs to be queued.
- Begin playback immediately when idle.
- Provide a live Now Playing experience.
- Display album artwork.
- Provide an administrative interface.
- Provide QR-code onboarding.
- Provide an Up Next view.
- Continue operating locally without dependence on a cloud music service.
- Separate library construction from the runtime appliance.

## 1.3 User Model

There are two roles:

### Guest

Guests are anonymous.

No guest account, registration, profile, or persistent identity is required.

Anonymity is intentional because identifying who queued a song is part of the party-game experience.

Guests may:

- Browse artists.
- Browse albums.
- Browse songs.
- Search the library.
- Queue songs.
- View Now Playing.
- View Up Next.
- Scan the QR code to reach Encore.

### Administrator

Administrators have authenticated access to operational controls.

Administrators may:

- Control playback.
- Skip tracks.
- Inspect queue state.
- Monitor system health.
- Inspect library information.
- View runtime statistics.
- Review logs.
- View configuration.
- Perform approved maintenance operations.

Guest anonymity must not be compromised by the administrative interface.

## 1.4 Operating Environment

Primary target:

- Raspberry Pi 4
- Linux
- Local LAN
- Hostname: `jukebox`
- Fully qualified local hostname: `jukebox.home.arpa`

All guests on the LAN should be able to access Encore.

## 1.5 Appliance Philosophy

Encore should:

- Start automatically.
- Recover from expected failures.
- Require minimal maintenance.
- Have predictable resource usage.
- Keep runtime state separate from library construction.
- Provide clear health information.
- Avoid unnecessary dependencies.

The jukebox should feel like an appliance, not a web application requiring continuous administration.

## 1.6 Functional Requirements

The system shall provide:

1. Offline library construction.
2. Metadata extraction and normalization.
3. Optional metadata repair using MusicBrainz.
4. Artwork handling.
5. SQLite library generation.
6. Search by artist, album, and song.
7. Artist browsing.
8. Album browsing.
9. Song browsing.
10. FIFO queueing.
11. Duplicate queue entries.
12. Playback through mpv.
13. Playback recovery.
14. Live Now Playing updates.
15. Album artwork display.
16. Up Next display.
17. QR-code access.
18. Administrative controls.
19. Health monitoring.
20. Runtime statistics.
21. Installation and upgrade support.

## 1.7 Non-Goals for Version 1

The following are future capabilities:

- Ratings
- Favorites
- Smart playlists
- Remote display clients
- Advanced recommendations
- AI-driven music discovery
- Cloud-dependent playback

## 1.8 Performance Goals

Initial targets:

- Search: less than 100 ms under normal library conditions.
- Queue operation: less than 50 ms.
- Typical HTMX navigation: less than 200 ms.
- SSE state propagation: less than 1 second.
- Playback initiation: approximately 250 ms target where hardware and media permit.

Performance shall be measured rather than assumed.

## 1.9 Acceptance Criteria

Version 1 is successful when a party guest can:

1. Connect to `jukebox.home.arpa`.
2. Browse artists.
3. Select an artist.
4. Select an album or song.
5. Queue a song.
6. See the song reflected in Up Next.
7. Observe Now Playing changes without refreshing the page.
8. See album artwork.
9. Continue using the system from a mobile device.
10. Queue the same song more than once when desired.

---

# Chapter 2 — System Architecture

## 2.1 Architectural Model

Encore consists of two major applications:

### Library Builder

An offline process that discovers, processes, validates, and publishes the music library.

### Jukebox Server

A continuously running appliance that consumes the published library and manages playback and user interaction.

The two applications are intentionally separated.

## 2.2 High-Level Architecture

```text
                Music Files
                    |
                    v
             +--------------+
             | Library      |
             | Builder      |
             +--------------+
                    |
                    v
              library.db
                    |
                    v
        +-----------------------+
        |     Encore Server     |
        |                       |
        | Search / Library      |
        | Queue                 |
        | Playback              |
        | Event Bus             |
        | HTTP / HTMX / SSE     |
        +-----------------------+
             |            |
             v            v
          mpv Audio     Browsers
```

## 2.3 Architectural Principles

- Separate construction from runtime.
- Keep `library.db` immutable at runtime.
- Keep mutable runtime state in `runtime.db`.
- Use clear service boundaries.
- Prefer events for cross-service state propagation.
- Keep the UI thin.
- Keep business rules outside controllers.
- Keep infrastructure behind interfaces.
- Favor simple, observable components.
- Design for failure recovery.

## 2.4 Runtime Components

Core components include:

- Configuration Service
- Library Service
- Search Service
- Queue Service
- Playback Service
- Playback Supervisor
- Statistics Service
- Health Service
- Event Bus
- SSE Publisher
- HTTP/API layer
- Administrative interface

## 2.5 Builder Components

The Library Builder contains:

- File discovery
- Metadata extraction
- Normalization
- MusicBrainz enrichment
- Artwork processing
- Duplicate analysis
- Database construction
- Search index construction
- Validation
- Publication

The builder must not depend on runtime playback components.

---

# Chapter 3 — Technology Stack & Development Environment

## 3.1 Required Stack

Backend:

- Python 3.12+
- FastAPI
- Pydantic
- SQLAlchemy 2.x
- SQLite
- SQLite FTS5

Frontend:

- Jinja2
- HTMX
- Tailwind CSS
- Server-Sent Events

Playback:

- mpv
- mpv JSON IPC

Library processing:

- Mutagen
- Pillow
- MusicBrainz services during offline enrichment

Development:

- uv
- pytest
- Ruff
- MyPy
- pre-commit
- GitHub Actions

Deployment:

- Linux
- systemd

## 3.2 Frontend Architecture

Encore shall not use a SPA framework.

Do not introduce:

- React
- Vue
- Angular
- client-side routing

Use server-rendered HTML enhanced with HTMX.

Use SSE for real-time state updates.

## 3.3 Backend Architecture

The backend shall use dependency injection and clear service ownership.

HTTP controllers should translate requests into application operations. They should not contain domain business logic.

## 3.4 Development Principles

- Strong typing.
- Automated formatting and linting.
- Small focused modules.
- Reproducible dependencies.
- Automated testing.
- Structured logging.
- Documentation alongside implementation.

---

# Chapter 4 — Domain Model

## 4.1 Core Entities

The core music domain consists of:

- Artist
- Album
- Song
- Artwork
- Music File
- Metadata
- Queue Item
- Playback State
- Playback History

## 4.2 Artist

An Artist represents a normalized music artist.

Properties include:

- Stable identifier
- Display name
- Sort name
- Normalized name
- Optional MusicBrainz identifier
- Artwork references where available

## 4.3 Album

An Album belongs to an Artist and represents a normalized release.

Properties include:

- Stable identifier
- Album title
- Sort title
- Normalized title
- Artist association
- Optional MusicBrainz release identifier
- Artwork reference

## 4.4 Song

A Song represents a playable track.

Properties include:

- Stable identifier
- Title
- Sort title
- Album association
- Artist association
- Track number
- Disc number
- Duration
- File path
- File format
- Metadata
- Artwork association

## 4.5 Queue Item

A Queue Item represents one request to play a song.

Each queue item is independent.

Two queue items may reference the same song.

No queue deduplication is permitted.

A queue item has:

- Unique queue identifier
- Song identifier
- Enqueue timestamp
- Position/order
- Playback status

## 4.6 Playback State

Playback state represents what the playback engine is currently doing.

States include:

- Idle
- Loading
- Playing
- Paused
- Stopping
- Error
- Recovering

## 4.7 Anonymous Guest

Guests do not have persistent application identities.

The system should not attempt to infer or expose which guest queued a song.

---

# Chapter 5 — Database Architecture & Storage Model

## 5.1 Database Separation

Encore uses two SQLite databases.

### `library.db`

Immutable after publication.

Contains the canonical music library and search structures.

### `runtime.db`

Mutable.

Contains queue, playback history, statistics, and other operational state.

## 5.2 Library Database Principles

`library.db` is produced by the Library Builder.

The runtime server opens it read-only.

The runtime server must never perform INSERT, UPDATE, DELETE, or schema changes against `library.db`.

## 5.3 Normalized Canonical Tables

The library uses normalized tables as the authoritative source.

Conceptually:

```text
artists
albums
songs
artwork
music_files
```

Relationships shall be normalized and enforced with foreign keys.

## 5.4 Denormalized Search Structures

The database shall also contain denormalized search structures optimized for read performance.

These may include:

- Artist search views/tables
- Album search views/tables
- Song search views/tables
- FTS5 virtual tables

Denormalized search structures are derived data.

The normalized tables remain authoritative.

## 5.5 Full Text Search

SQLite FTS5 shall support:

- Artist search
- Album search
- Song search

Search must support partial and token-based matching appropriate for music titles and artist names.

## 5.6 Artwork

Artwork may be stored as files with database references rather than embedding large binary objects directly into relational rows.

The database must provide stable artwork references.

## 5.7 Runtime Database

Runtime state includes:

- Queue items
- Playback history
- Runtime statistics
- Administrative state as required

The runtime database is mutable and may be rebuilt or migrated independently of the music library.

## 5.8 Atomic Library Publication

The builder must publish a completed database atomically.

A partially constructed library must never become the active runtime library.

## 5.9 Acceptance Criteria

- Library database can be opened read-only by the server.
- Runtime operations never modify library data.
- Search structures are populated and validated.
- Foreign key relationships are valid.
- Database migrations are versioned.
- A failed build cannot replace a valid published library.

---

# Chapter 6 — Library Builder

## 6.1 Purpose

The Library Builder converts a collection of music files into a validated, optimized, immutable Encore library.

It is an offline process.

## 6.2 Pipeline

The builder uses staged processing:

```text
Discovery
   |
Extraction
   |
Normalization
   |
Enrichment
   |
Artwork Generation
   |
Duplicate Analysis
   |
Database Construction
   |
Search Optimization
   |
Validation
   |
Publication
```

## 6.3 Discovery

The builder recursively discovers supported files:

- MP3
- FLAC
- AAC
- M4A

Unsupported files are ignored or reported.

## 6.4 Metadata Extraction

Embedded tags are the primary metadata source.

Extract:

- Artist
- Album Artist where available
- Album
- Title
- Track number
- Disc number
- Genre
- Date
- Duration
- Embedded artwork
- MusicBrainz identifiers when present

## 6.5 Normalization

Metadata is normalized for consistent search and display.

Normalization should address:

- Whitespace
- Case-insensitive comparison
- Empty values
- Track/disc numbering
- Artist and album naming consistency

Original metadata should remain available where appropriate.

## 6.6 MusicBrainz Enrichment

MusicBrainz may be used to repair missing or incomplete metadata.

Enrichment is:

- Offline-builder-only.
- Optional.
- Rate-limited.
- Non-blocking where practical.

A MusicBrainz outage must not prevent processing otherwise valid music.

## 6.7 Artwork

The builder should:

1. Prefer embedded artwork.
2. Normalize artwork format/size as required.
3. Cache generated artwork.
4. Associate artwork with artists/albums/songs as appropriate.

Missing artwork must not make a track unplayable.

## 6.8 Duplicate Analysis

The builder may identify duplicate files or suspicious metadata.

Duplicate analysis is informational unless a future explicit policy says otherwise.

The builder must not silently delete user music.

## 6.9 Incremental Builds

The builder should avoid unnecessary work when files and metadata have not changed.

Build checkpoints should allow efficient recovery.

## 6.10 Validation

Before publication, validate:

- Database schema.
- Foreign keys.
- Song file paths.
- Metadata consistency.
- Artwork references.
- Search indexes.
- Required fields.
- Database integrity.

## 6.11 Publication

Publication must be atomic.

A successful build produces a complete library artifact and associated manifest/report.

A failed build leaves the previous valid library untouched.

## 6.12 Build Reports

The builder shall report:

- Files discovered
- Files processed
- Files skipped
- Metadata repaired
- Artwork generated
- Warnings
- Errors
- Duplicates/suspected duplicates
- Final song count
- Database validation result

---

# Chapter 7 — Playback Service

## 7.1 Responsibility

The Playback Service is the only component that directly controls the audio playback engine.

mpv is the selected playback engine.

## 7.2 Playback Supervisor

A Playback Supervisor shall:

- Launch mpv.
- Monitor the process.
- Establish IPC.
- Detect crashes.
- Restart mpv.
- Reconnect IPC.
- Report health.

## 7.3 Playback State Machine

```text
Idle
 |
Loading
 |
Playing
 | \
 |  Paused
 |
Finished
 |
Idle
```

Error/recovery paths may transition through:

```text
Error -> Recovering -> Idle/Playing
```

## 7.4 Commands

Playback shall support:

- Play
- Pause
- Resume
- Stop
- Skip
- Seek where appropriate
- Load track

Commands shall be asynchronous where required to prevent blocking the server.

## 7.5 Progress

Playback progress should be available approximately once per second.

Progress updates should be published as immutable events.

## 7.6 Automatic Recovery

If mpv terminates unexpectedly:

1. Detect failure.
2. Record diagnostic information.
3. Publish health/recovery events.
4. Restart mpv.
5. Reconnect IPC.
6. Restore appropriate playback state.
7. Continue queue processing where possible.

## 7.7 Audio Configuration

Audio output configuration is installation/runtime configuration.

Playback code must not embed hardware-specific assumptions.

## 7.8 Gapless and Crossfade

Playback should support gapless transitions where media and mpv permit.

Crossfade behavior should be configurable and implemented through the playback layer.

## 7.9 Acceptance Criteria

- mpv is isolated behind the Playback Service.
- mpv crashes are detected and recoverable.
- Playback state is observable.
- Queue management remains separate from audio engine control.
- No HTTP/UI code directly communicates with mpv.

---

# Chapter 8 — Queue & Playback Orchestration

## 8.1 Queue Model

Encore uses a strict FIFO queue.

The first queued item is the next item played.

No priority queue is permitted for guest requests.

## 8.2 Queue Insertion

When a guest selects a song:

```text
No song playing
    -> begin playback

Song already playing
    -> append to FIFO queue
```

## 8.3 Duplicate Queue Items

**Queue deduplication is explicitly prohibited.**

If two guests intentionally or accidentally select the same song twice, both requests remain in the queue.

Example:

```text
Guest A -> Song X
Guest B -> Song X

Queue:

Song X
Song X
```

These are two legitimate queue entries.

## 8.4 Queue Size

Normal queue target:

- Approximately 25–50 items.

The implementation may enforce an operational maximum, but the maximum must be explicit and user-visible where applicable.

## 8.5 Queue Ordering

Queue ordering is determined by enqueue order.

Ordering must remain stable across:

- UI refreshes.
- SSE updates.
- Server restarts where persisted queue state is restored.

## 8.6 Queue Persistence

The queue resides in `runtime.db`.

A restart should preserve valid queued items when practical.

## 8.7 Playback Advancement

When a song completes:

1. Publish completion event.
2. Remove/mark the completed queue item.
3. Select the next FIFO item.
4. Begin playback.
5. Publish SongStarted.

## 8.8 Administrative Controls

Administrators may:

- Skip current track.
- Stop playback.
- Pause/resume.
- Inspect queue.
- Remove queue items as an administrative operation.

Administrative removal does not alter FIFO semantics for remaining items.

## 8.9 Queue Events

The queue shall publish events for:

- Song queued
- Song removed
- Queue advanced
- Queue emptied
- Playback started
- Playback completed

---

# Chapter 9 — Web User Interface

## 9.1 Design Principle

The web interface is one continuous guest experience.

It should feel like a single mobile application even though it is server-rendered.

## 9.2 Mobile-First

The primary user device is a smartphone.

The interface must:

- Work on small screens.
- Use large touch targets.
- Avoid unnecessary typing.
- Avoid horizontal scrolling.
- Load quickly on a local wireless network.

## 9.3 Persistent Shell

The UI should maintain a consistent shell containing:

- Header/navigation.
- Main content area.
- Persistent access to Now Playing/queue as appropriate.
- Footer or supporting navigation where useful.

HTMX swaps content within the shell.

## 9.4 Discovery

Guests can discover music through:

- Artist browsing.
- Album browsing.
- Song browsing.
- Search.

## 9.5 Artist Navigation

Artist selection should populate that artist's:

- Albums.
- Songs.

The user should not need to repeatedly submit forms or navigate through unnecessary pages.

## 9.6 Search

Search supports:

- Artist
- Album
- Song

Results should clearly identify what matched.

## 9.7 Song Selection

Selecting a song produces one queue request.

The user should receive immediate visual feedback.

If nothing is playing, the UI should indicate that playback has started or is starting.

If something is playing, the UI should indicate that the song has been added to Up Next.

## 9.8 Live View

The Live/Now Playing view shall display:

- Artist
- Song title
- Album
- Album artwork
- Playback state
- Progress where appropriate

The view must update without requiring a full page reload.

## 9.9 Up Next

The Up Next view shows the FIFO queue.

Guests may see upcoming songs but must not see guest identities.

## 9.10 Real-Time Updates

Use SSE for:

- Now Playing changes.
- Playback state.
- Progress.
- Queue updates.
- Health/status information where appropriate.

HTMX should consume server-rendered updates where practical.

## 9.11 QR Code

Encore shall provide a QR code that points to the guest interface.

The QR code is intended to be displayed at the physical jukebox.

The expected guest workflow is:

```text
Scan QR
   |
Open jukebox.home.arpa
   |
Browse/Search
   |
Queue music
```

## 9.12 Visual Design

The UI should be:

- Attractive.
- Modern.
- Uncluttered.
- High contrast.
- Touch-friendly.
- Fast.

The design should emphasize album artwork and music discovery rather than application controls.

---

# Chapter 10 — HTTP API & Real-Time Communication

## 10.1 API Architecture

Encore provides two complementary HTTP interfaces:

1. HTMX-oriented HTML fragment endpoints.
2. A versioned JSON API.

## 10.2 JSON API

The API is versioned under:

```text
/api/v1/
```

It shall provide machine-readable access to core operations.

## 10.3 Resource Areas

The API should cover:

- Artists
- Albums
- Songs
- Search
- Queue
- Playback state
- Now Playing
- Health
- Statistics
- Artwork
- QR-code information

## 10.4 Queue API

Queue insertion is a non-idempotent operation.

Two identical requests must create two queue items.

No deduplication shall occur.

## 10.5 Server-Sent Events

SSE endpoint:

```text
/events
```

The SSE stream should publish relevant state changes.

Events include:

- SongQueued
- SongStarted
- SongFinished
- QueueAdvanced
- PlaybackRecovered
- HealthChanged
- LibraryReloaded

## 10.6 HTMX Endpoints

HTMX endpoints return HTML fragments designed for direct DOM replacement.

They should not return application JSON when an HTML fragment is the appropriate interface.

## 10.7 Error Handling

Errors use consistent HTTP status codes and structured error responses for JSON endpoints.

Examples:

- 400 — invalid request
- 401 — unauthenticated administrator request
- 403 — unauthorized operation
- 404 — resource not found
- 409 — state conflict where applicable
- 422 — validation error
- 429 — rate limit
- 500 — unexpected server error
- 503 — unavailable service

## 10.8 Authentication

Guest endpoints do not require authentication.

Administrative endpoints require authentication.

Guest access must never expose administrative operations.

## 10.9 Rate Limiting

Guest operations should have reasonable protection against accidental or abusive request floods without making normal party usage inconvenient.

## 10.10 Caching

Artwork and immutable library resources should be cacheable.

Mutable queue and playback state must not be cached incorrectly.

## 10.11 OpenAPI

The JSON API shall produce an OpenAPI specification from the application.

---

# Chapter 11 — System Architecture & Service Composition

## 11.1 Internal Event Bus

Encore uses an internal in-process Event Bus.

It is not a network message broker.

The Event Bus exists to decouple runtime services while keeping the appliance architecture simple.

## 11.2 Event Characteristics

Events shall be:

- Immutable.
- Typed.
- Timestamped.
- Ordered within the applicable event stream.
- Independent of HTTP.

## 11.3 Subscribers

Subscribers register with the Event Bus.

One subscriber failure must not prevent unrelated subscribers from receiving events.

## 11.4 Synchronous and Asynchronous Work

The implementation may support both synchronous and asynchronous subscribers.

Long-running handlers must not block critical playback operations.

## 11.5 Dependency Injection

Services receive their dependencies explicitly.

Avoid hidden global service state.

Repositories and infrastructure adapters should be injectable.

## 11.6 Service Ownership

Each service owns one clear responsibility.

Examples:

- Queue Service owns queue rules.
- Playback Service owns mpv.
- Search Service owns search behavior.
- Library Service owns access to immutable library data.
- Health Service owns health aggregation.

## 11.7 Startup

Startup should:

1. Load configuration.
2. Validate environment.
3. Open databases.
4. Initialize repositories.
5. Initialize Event Bus.
6. Initialize playback supervisor.
7. Start HTTP server.
8. Publish ready/health state.

## 11.8 Shutdown

Shutdown should:

1. Stop accepting new work.
2. Gracefully close SSE connections where practical.
3. Stop playback.
4. Close mpv IPC.
5. Flush required runtime state.
6. Close databases.
7. Exit cleanly.

## 11.9 Failure Isolation

A failure in one subsystem should not unnecessarily terminate the entire appliance.

Examples:

- SSE client failure must not affect playback.
- Artwork failure must not stop playback.
- One Event Bus subscriber failure must not stop other subscribers.
- mpv failure should invoke recovery rather than terminate the server.

## 11.10 Architectural Guardrails

The following rules are mandatory:

- Domain services shall not import FastAPI.
- Domain services shall not depend on HTMX.
- Controllers shall not access SQLite directly.
- Repositories shall not contain business rules.
- Event handlers shall not directly invoke other event handlers.
- Playback code shall not know about HTTP.
- Library Builder shall not import runtime playback components.
- Runtime services shall never modify `library.db`.
- Templates shall contain presentation logic only.

---

# Chapter 12 — Configuration & Runtime State

## 12.1 Configuration Philosophy

Encore uses a simple installation-time configuration model.

Configuration is intended to be:

**set it and forget it.**

The system must not turn configuration into an application-managed database.

## 12.2 YAML Configuration

A YAML configuration file may be supplied at installation/deployment time.

It contains operational configuration such as:

- Audio output.
- Library location.
- Runtime database location.
- Artwork location.
- Network/server settings.
- Queue limits.
- Playback options.

## 12.3 Configuration Is Not Managed State

The YAML file is not application-managed state.

Encore must not provide an administrative interface that edits and persists the YAML configuration.

The administrative interface may display a read-only configuration summary.

## 12.4 Runtime State

Runtime state belongs in `runtime.db`.

Examples:

- Queue.
- Playback history.
- Runtime statistics.
- Current operational state where persistence is required.

## 12.5 Configuration Validation

Configuration is validated at startup.

Invalid configuration should prevent startup with a clear diagnostic.

## 12.6 Secrets

Secrets must not be hard-coded into source code.

Sensitive values should use appropriate environment/system mechanisms where required.

## 12.7 No YAML as Database

Do not use YAML for:

- Queue state.
- Playback state.
- Statistics.
- User state.
- Library state.
- Arbitrary application-managed records.

---

# Chapter 13 — Deployment & Installation

## 13.1 Supported Platforms

Primary:

- Raspberry Pi 4
- Raspberry Pi OS 64-bit

Secondary:

- Debian 12
- Ubuntu 24.04 LTS
- Other modern systemd Linux distributions where practical.

## 13.2 Installation Methods

Encore supports:

1. Automated installation.
2. Manual installation.

The manual installation documentation is authoritative.

The automated installer automates the same process.

## 13.3 Filesystem Layout

Recommended layout:

```text
/opt/encore/
    application/
    venv/

/etc/encore/
    config.yaml

/var/lib/encore/
    library.db
    runtime.db
    artwork/

/var/cache/encore/
    temp/

/var/log/encore/
```

## 13.4 Python Environment

Encore runs inside a dedicated Python virtual environment.

System Python packages must not be modified unnecessarily.

## 13.5 Service User

Encore runs as a dedicated unprivileged service account, such as:

```text
encore
```

It must not run as root.

## 13.6 systemd

Encore shall provide a native systemd service.

The service should:

- Start at boot.
- Depend on appropriate network readiness.
- Restart on failure where appropriate.
- Shut down gracefully.
- Run under the dedicated service user.

## 13.7 Installer

`encore-install` shall:

- Verify prerequisites.
- Create required directories.
- Create the service user if necessary.
- Install Python dependencies.
- Install Encore.
- Install systemd integration.
- Validate configuration.
- Report next steps.

The installer must be idempotent.

## 13.8 Non-Destructive Behavior

By default, installation and upgrade must preserve:

- Configuration.
- Library database.
- Runtime database.
- Artwork.
- Logs.

Potentially destructive operations require explicit action.

## 13.9 Upgrade

Upgrade should:

1. Stop Encore.
2. Update application.
3. Apply required migrations.
4. Validate installation.
5. Start Encore.

Existing user data remains intact.

## 13.10 Removal

Removal should distinguish between:

- Application removal.
- Configuration removal.
- Runtime data removal.

User data should not be deleted implicitly.

## 13.11 Acceptance Criteria

A successful installation results in:

- Running systemd service.
- Accessible guest interface.
- Accessible administrative interface.
- Functional playback.
- Valid library access.
- Passing health checks.

---

# Chapter 14 — Testing Strategy & Quality Assurance

## 14.1 Quality Philosophy

Encore must be:

- Predictable.
- Reliable.
- Recoverable.
- Repeatable.
- Observable.

Testing is a first-class engineering activity.

## 14.2 Testing Pyramid

```text
            End-to-End
           Integration
          Component Tests
           Unit Tests
```

## 14.3 Test Categories

The project shall include:

- Unit tests.
- Component tests.
- Integration tests.
- API tests.
- UI tests.
- Playback tests.
- Database tests.
- Performance tests.
- Recovery tests.
- Party Simulation.
- Regression tests.

## 14.4 Unit Testing

Core services must have comprehensive unit coverage.

Unit tests should not require:

- Audio hardware.
- Network connectivity.
- Real mpv.
- External MusicBrainz access.

## 14.5 Integration Testing

Test interactions between:

- Queue and Playback.
- Playback and Event Bus.
- Event Bus and SSE.
- Library and Search.
- Runtime database and Queue.

## 14.6 API Testing

Test:

- Success.
- Validation failures.
- Authorization.
- Malformed requests.
- Unsupported methods.
- Response schemas.

## 14.7 UI Testing

Test realistic guest workflows:

- Browse artists.
- Browse albums.
- Search.
- Queue songs.
- View Now Playing.
- View Up Next.
- Reconnect after network interruption.

## 14.8 Playback Testing

Test:

- mpv startup.
- IPC.
- Track transitions.
- Pause/resume.
- Skip.
- Progress.
- Recovery after mpv failure.

## 14.9 Performance Testing

Test against a representative 15,000-song library.

Measure search, queue operations, navigation, SSE propagation, startup, and resource consumption.

## 14.10 Recovery Testing

Simulate:

- mpv crashes.
- Network interruption.
- Browser disconnect.
- Missing media files.
- Missing artwork.
- Interrupted library publication.
- Service restarts.

## 14.11 Party Simulation Suite

Encore shall include an automated Party Simulation suite representing a realistic party.

Baseline profile:

- 40 simultaneous simulated guests.
- 15,000-song library.
- Continuous searching.
- Artist browsing.
- Album browsing.
- Song queueing.
- Playback progression.
- SSE updates.
- Administrative activity.

The simulation should run for an extended period, such as one hour.

## 14.12 Load Profiles

Provide profiles for:

- 5 guests — small gathering.
- 25 guests — house party.
- 50 guests — large party.
- 100+ guests — stress test.

## 14.13 Chaos Testing

Controlled failures should include:

- mpv termination.
- Subscriber failure.
- Network client disconnects.
- Music-file removal.
- SSE reconnects.
- Service restart.

## 14.14 Regression Rule

Every fixed defect must produce a permanent regression test.

## 14.15 Continuous Integration

Pull requests should run:

- Formatting.
- Linting.
- Type checking.
- Unit tests.
- Component/integration tests.
- API tests.

Long-running performance and Party Simulation workloads may run on scheduled workflows.

## 14.16 Coverage

Target meaningful coverage of approximately:

- Domain services: 95%.
- Queue: 95%.
- Playback: 90%.
- Search: 95%.
- Library Builder: 90%.
- Overall: at least 90%.

Coverage is an indicator, not a substitute for meaningful tests.

---

# Chapter 15 — Development Standards & Project Structure

## 15.1 Engineering Principles

Encore development favors:

- Readability.
- Simplicity.
- Composition.
- Explicit behavior.
- Strong typing.
- Small focused modules.
- Testability.
- Maintainability.

## 15.2 Clean Architecture

Dependency direction flows inward:

```text
Presentation
    |
Application
    |
Domain
    |
Repositories
    |
Infrastructure
```

Higher-level layers must not be imported by lower-level domain layers.

## 15.3 Architectural Guardrails

Mandatory rules:

- Domain services do not import FastAPI.
- Domain services do not import HTMX-specific code.
- Controllers do not access SQLite directly.
- Repositories do not contain business rules.
- Event handlers do not invoke each other directly.
- Playback does not know about HTTP.
- Library Builder does not import runtime playback.
- Runtime does not modify `library.db`.
- Templates contain presentation logic only.

## 15.4 Project Layout

```text
encore/
├── apps/
│   ├── server/
│   └── builder/
├── encore/
│   ├── api/
│   ├── config/
│   ├── controllers/
│   ├── domain/
│   ├── events/
│   ├── playback/
│   ├── repositories/
│   ├── search/
│   ├── services/
│   ├── templates/
│   ├── static/
│   └── utilities/
├── tests/
├── docs/
├── adr/
└── scripts/
```

## 15.5 Python Standards

Use:

- Python 3.12+.
- Type hints.
- pathlib.
- Dataclasses/Pydantic where appropriate.
- Enums for fixed values.
- Context managers for resources.

## 15.6 Naming

Classes use PascalCase.

Functions and modules use snake_case.

Constants use uppercase snake case.

Names should describe intent.

## 15.7 Documentation

Public classes, functions, and modules should have concise documentation.

Comments should explain why rather than merely restating what code does.

## 15.8 Architectural Decision Records

Significant architectural decisions must be recorded in `docs/adr/`.

Each ADR includes:

- Title.
- Status.
- Date.
- Context.
- Decision.
- Consequences.
- Alternatives considered.

## 15.9 Initial ADRs

Initial decisions include:

- Separate Library Builder and Server.
- HTMX rather than SPA.
- SQLite storage.
- Internal Event Bus.
- mpv playback.
- Immutable library database.
- Anonymous guest model.
- Appliance-first philosophy.

## 15.10 Dependency Management

Dependencies should be:

- Minimal.
- Maintained.
- Justified.
- Reproducible.
- License-compatible.

## 15.11 Git Workflow

```text
Issue
  |
Feature Branch
  |
Development
  |
Tests
  |
Pull Request
  |
Review
  |
Merge
```

`main` should remain deployable.

## 15.12 Commit Messages

Use a consistent convention such as:

```text
feat(queue): add FIFO queue service
fix(playback): recover after mpv crash
docs(adr): document event bus architecture
test(search): add album lookup regression
```

## 15.13 Pull Requests

PRs should contain:

- Description.
- Motivation.
- Tests.
- Documentation updates.
- Related issue.
- Architectural impact where applicable.

## 15.14 AI-Assisted Development

AI-generated contributions must:

- Follow this specification.
- Follow the AIG and AEP.
- Pass tests.
- Respect architectural guardrails.
- Include documentation where appropriate.
- Receive appropriate human review.

## 15.15 Licensing

The project should use a permissive open-source license selected before the public release.

Candidate licenses include MIT, BSD 3-Clause, or Apache 2.0.

---

# Chapter 16 — Roadmap & Project Manifesto

## 16.1 Vision

Encore exists to make sharing music effortless.

A guest should be able to:

1. Join the local network.
2. Scan a QR code.
3. Find a song.
4. Add it to the queue.
5. Enjoy the music.

Nothing else should be required.

## 16.2 Version 1.0 — Foundation

Version 1.0 establishes the complete appliance:

- Offline Library Builder.
- Immutable library database.
- Mobile-first HTMX interface.
- Search.
- Artist/album/song browsing.
- Anonymous guest model.
- FIFO queue.
- Duplicate queue entries.
- mpv playback.
- Administrative dashboard.
- Metadata enrichment.
- SSE.
- QR onboarding.
- Up Next.
- Event-driven architecture.
- Raspberry Pi deployment.

Stability takes priority over feature count.

## 16.3 Version 1.1 — Music Discovery

Potential features:

- Favorites.
- Ratings.
- Recently added.
- Recently played.
- Frequently played statistics.
- Improved artwork presentation.
- Music discovery enhancements.

## 16.4 Version 1.2 — Party Enhancements

Potential features:

- Smart playlists.
- Party themes.
- Continuous-mix improvements.
- Crossfade improvements.
- Visualizers.
- Queue duration estimates.
- Additional mobile polish.

## 16.5 Version 2.0 — Connected Home

Potential integrations:

- Home Assistant.
- MQTT.
- Telegram.
- Discord.
- Companion applications.
- Expanded REST API.
- Plugin API.

## 16.6 Version 2.x — Remote Displays

Potential clients:

- Dedicated Now Playing displays.
- TVs.
- Kitchen displays.
- Raspberry Pi display clients.
- Digital photo frames.

These should consume documented APIs and events.

## 16.7 Version 3.x — Intelligent Features

Potential capabilities:

- Queue suggestions.
- Duplicate-artist spacing recommendations.
- Mood-aware playlists.
- Similar-artist recommendations.
- Semantic search.
- Natural-language search.
- Voice interaction.
- Automated library cleanup suggestions.

AI should augment discovery rather than replace user choice.

## 16.8 Community

The project should remain:

- Friendly.
- Welcoming.
- Helpful.
- Respectful.
- Technically curious.

## 16.9 Contributor Experience

Contributors should be able to:

- Build quickly.
- Understand the architecture.
- Run tests.
- Make focused changes.
- Submit confident pull requests.

## 16.10 Plugin Ecosystem

Potential plugins include:

- Home Assistant.
- MQTT.
- Discord.
- Telegram.
- Statistics exporters.
- Party analytics.
- LED synchronization.
- Notifications.

Plugins should use documented APIs and Event Bus interfaces rather than modifying core internals.

## 16.11 Internationalization

Future support may include:

- Multiple languages.
- RTL layouts.
- Locale-aware formatting.
- Language packs.

## 16.12 Performance

Future optimization should target:

- Faster library loading.
- Lower memory consumption.
- Lower CPU utilization.
- Faster indexing.
- Faster startup.
- Efficient artwork caching.

Performance improvements must not sacrifice clarity.

## 16.13 Security

Future security improvements may include:

- HTTPS by default.
- Certificate management.
- Stronger administrative authentication.
- Two-factor authentication.
- Security audit logging.
- Additional hardening guidance.

Guest simplicity must remain intact.

## 16.14 Platform Support

The architecture should remain portable to:

- Raspberry Pi.
- Mini PCs.
- Home servers.
- NAS systems.
- Virtual machines.
- Containers.

## 16.15 Polish Release

A future release may focus exclusively on refinement:

- UI consistency.
- Accessibility.
- Navigation speed.
- Onboarding.
- Documentation.
- Resource usage.
- Installer quality.
- Administrative UX.

## 16.16 Success Metrics

Encore succeeds when:

- Guests can use it without instructions.
- Music starts quickly.
- The interface feels responsive.
- Failures recover gracefully.
- Contributors understand the architecture.
- New features integrate cleanly.
- The project remains enjoyable to maintain.

---

# Project Manifesto

## Encore Manifesto

Encore exists for one purpose:

**To bring people together through music.**

Technology should never become the center of attention.

A guest should remember the song they discovered, the conversation they had, and the memories they made—not the software they used.

Encore is built on a few simple beliefs:

- Music libraries deserve to be enjoyed.
- Simplicity is a feature.
- Reliability is more important than novelty.
- Fast software respects the user's time.
- Anonymous participation makes parties more fun.
- Open standards create lasting software.
- Clean architecture enables long-term sustainability.
- Every contributor should leave the project better than they found it.

Encore is not designed to imitate a streaming service.

It is designed to celebrate personal music collections.

Encore favors thoughtful engineering over unnecessary complexity.

It embraces open source, documentation, automated testing, and maintainable design.

It should be enjoyable to use.

It should be enjoyable to contribute to.

And above all:

**It should let the music play.**

---

# Document Acceptance Criteria

The SAPRS is considered the architectural baseline when:

- Product requirements are documented.
- Runtime and Builder responsibilities are separated.
- Data architecture is documented.
- API and real-time communication are defined.
- Event Bus behavior is defined.
- Configuration and runtime state are separated.
- Installation and deployment are defined.
- Testing and Party Simulation are defined.
- Development standards are defined.
- Initial architectural decisions are identified.
- Future roadmap and Project Manifesto are documented.

---

# Revision History

| Version | Status | Description |
|---|---|---|
| 1.0 | Implementation Baseline | Consolidated Encore Chapters 1–16 |
