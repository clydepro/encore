
# Encore AI Implementation Guide (AIG)

**Version:** 1.0

**Applies to:** Encore SAPRS v1.0

---

# 1. Purpose

This guide is the implementation blueprint for Encore.

Unlike the Software Architecture & Product Requirements Specification (SAPRS), this guide intentionally omits architectural discussion and rationale.

It defines exactly what must be built, how the project is organized, and the engineering rules that all implementations must follow.

This guide is the primary document provided to AI coding assistants.

The SAPRS remains the authoritative architectural reference.

---

# 2. Project Objectives

Implement a headless music jukebox appliance that:

- Runs on Raspberry Pi 4.
    
- Uses Python as the backend.
    
- Uses FastAPI.
    
- Uses HTMX.
    
- Uses Server-Sent Events.
    
- Uses SQLite.
    
- Uses mpv.
    
- Supports approximately 15,000 songs.
    
- Supports anonymous guests.
    
- Provides an administrative interface.
    
- Uses an immutable music library database.
    
- Separates offline library building from runtime playback.
    

---

# 3. Technology Stack

## Required

Python 3.12+

FastAPI

HTMX

Jinja2

Tailwind CSS

SQLite

FTS5

SQLAlchemy 2.x

Pydantic

mpv IPC

MusicBrainz (builder only)

Mutagen

Pillow

uv

systemd

---

# 4. Architecture Rules

The following rules are mandatory.

Domain services never import FastAPI.

Repositories never contain business logic.

Controllers never access SQLite directly.

Services communicate through interfaces or the Event Bus.

The runtime never modifies `library.db`.

Library Builder never imports runtime playback components.

Templates contain presentation only.

Every service has one responsibility.

All communication between services should be event-driven whenever practical.

---

# 5. Repository Layout

```text
apps/
    server/
    builder/

encore/
    api/
    config/
    controllers/
    domain/
    events/
    playback/
    repositories/
    search/
    services/
    templates/
    static/
    utilities/

tests/
    unit/
    integration/
    performance/
    regression/
    party_simulation/

docs/

adr/
```

This structure is mandatory.

---

# 6. Applications

The repository contains two independent applications.

## Builder

Produces:

library.db

Artwork cache

Build report

Validation report

Terminates after completion.

---

## Server

Loads:

library.db

runtime.db

Starts:

HTTP server

Event Bus

Playback

SSE

Administrative interface

Runs continuously.

---

# 7. Core Services

Implement the following services.

ConfigurationService

LibraryService

SearchService

QueueService

PlaybackService

PlaybackSupervisor

HealthService

StatisticsService

EventBus

SSEPublisher

LoggingService

Each service owns one capability.

---

# 8. Event Bus

The Event Bus is mandatory.

Publishers never know subscribers.

Subscribers never know publishers.

Events are immutable.

Initial events:

SongQueued

SongStarted

SongFinished

QueueAdvanced

PlaybackRecovered

LibraryReloaded

BuildCompleted

HealthChanged

---

# 9. Database

Implement two databases.

library.db

Read-only.

Contains:

Artists

Albums

Songs

Artwork

Metadata

FTS indexes

Search views

---

runtime.db

Read/write.

Contains:

Queue

Playback history

Runtime statistics

Administrative state

---

# 10. Playback

Playback engine:

mpv

Communication:

JSON IPC

Required capabilities:

Gapless playback

Crossfade

Pause

Resume

Skip

Stop

Progress reporting

Automatic recovery

---

# 11. Search

Search shall support:

Artist

Album

Song

Search latency target:

100 milliseconds

Implement using SQLite FTS5.

---

# 12. Queue

Strict FIFO.

Duplicates allowed.

Guests anonymous.

Playback begins immediately when idle.

Otherwise songs append to queue.

---

# 13. User Interface

Technology:

HTMX

Tailwind

Jinja2

SSE

No SPA framework.

No React.

No Vue.

No Angular.

No client-side routing.

---

# 14. Administrative Interface

Implement:

Login

Dashboard

Playback controls

Queue management

Health

Statistics

Logs

Configuration summary

Library information

---

# 15. HTTP API

Provide:

HTMX fragment endpoints

Versioned JSON API

Server-Sent Events

Generate OpenAPI automatically.

---

# 16. Library Builder

Read supported music files.

Extract metadata.

Repair metadata through MusicBrainz.

Generate optimized SQLite database.

Generate artwork.

Validate results.

Produce reports.

Never modify runtime databases.

---

# 17. Coding Standards

Python 3.12+

Type hints everywhere.

Constructor dependency injection.

Structured logging.

Small modules.

High cohesion.

Low coupling.

---

# 18. Testing Requirements

Every feature requires:

Unit tests

Integration tests

Regression tests

End-to-end tests

Party Simulation coverage when applicable

Every bug fixed becomes a regression test.

---

# 19. Performance Targets

Search:

<100 ms

Queue:

<50 ms

Navigation:

<200 ms

Playback start:

<250 ms

SSE:

<1 second

---

# 20. Definition of Done

A feature is complete only when:

Code implemented.

Tests pass.

Type checking passes.

Linting passes.

Documentation updated.

No architectural guardrails violated.

Acceptance criteria satisfied.

---

# 21. Implementation Order

The AI shall implement the project in the following order.

1. Repository initialization
    
2. Core domain model
    
3. Event Bus
    
4. Configuration
    
5. Repositories
    
6. Library Builder
    
7. Search
    
8. Playback
    
9. Queue
    
10. Runtime database
    
11. FastAPI
    
12. HTMX
    
13. SSE
    
14. Administrative interface
    
15. Installer
    
16. Party Simulation
    
17. Documentation
    

---

# 22. Never Do These Things

Do not introduce a SPA framework.

Do not store runtime state inside `library.db`.

Do not expose SQLite directly to controllers.

Do not bypass the Event Bus.

Do not put business logic in templates.

Do not couple Builder and Server.

Do not implement guest accounts.

Do not use polling where SSE is available.

Do not optimize before measuring.

Do not violate the architectural guardrails.

---

# 23. Final Deliverable

The completed project shall provide:

- A one-command installer (`encore-install`) plus fully documented manual installation.
    
- An offline Library Builder.
    
- An immutable music library database.
    
- A mobile-friendly anonymous jukebox.
    
- A responsive HTMX interface.
    
- A versioned JSON API.
    
- A real-time SSE interface.
    
- A secure administrative interface.
    
- A comprehensive automated test suite.
    
- A Party Simulation regression suite.
    
- Complete developer documentation.
    
