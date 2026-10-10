# Encore

**A headless music jukebox for the local network.** Guests scan a QR code, pick
songs from your library on their own phones, and a strictly FIFO queue plays
them through mpv. The box lives in a cupboard and needs no tending: the music,
not the software, is the centre of attention.

> **Status: bootstrap.** The repository is initialised, the tooling and CI are
> enforced, and the architecture is documented — no application code yet. See
> [Roadmap](#roadmap) and [Project Bootstrap Kit](docs/ProjectBootstrapKit_PBK.md).

## Table of contents

- [What is Encore?](#what-is-encore)
- [Why does it exist?](#why-does-it-exist)
- [Screenshots](#screenshots)
- [Features](#features)
- [Architecture overview](#architecture-overview)
- [Installation](#installation)
- [Development](#development)
- [Documentation map](#documentation-map)
- [Roadmap](#roadmap)
- [License](#license)
- [Contributing](#contributing)

## What is Encore?

An appliance, not an app. Encore runs on a Raspberry Pi 4 attached to your
speakers and serves a mobile-first web interface to everyone on the LAN.

- **Library** — roughly 15,000 songs of MP3, FLAC, AAC or M4A, indexed offline
  into an immutable SQLite database with FTS5 search.
- **Guests** — anonymous. No accounts, no app, no personal data stored.
- **Queue** — strict FIFO, visible to everyone, duplicates allowed.
- **Playback** — mpv with gapless transitions, crossfade, progress reporting and
  automatic recovery from crashes.
- **Administration** — an authenticated dashboard for playback, health,
  statistics and logs.
- **Real time** — Server-Sent Events drive Now Playing and Up Next; the page
  never polls.

## Why does it exist?

Streaming services solved access and created three problems at a party: accounts
and paywalls at the door, a shared device that nobody wants to hand over, and a
cloud dependency that fails when the Wi-Fi does.

Encore answers with something closer to a record collection: your files, your
speakers, your local network. Anyone in the room can add to the queue in about
five seconds, nobody has to log in, nobody's listening history is stored, and
the queue is public and fair so the room can police itself. When the internet
goes down, the music does not.

It is also a deliberately small system: one Python process per role, one
database per kind of state, no brokers, no SPA framework, no containers
required. Every dependency has to justify itself (SAPRS 15.10), which is the
same rule that keeps the appliance dependable.

## Screenshots

Not yet — the interface exists (milestones 11–13, `encore/templates/`), but no
screenshots of it have been taken. Images will land in
[`docs/images/`](docs/images/) and be referenced from the
[User Guide](docs/User-Guide.md).

## Features

**Guest experience**

- Mobile-first browsing by artist, album and song.
- Sub-100 ms search over the whole library (FTS5, token and partial matching).
- One-tap queueing, including repeats of a track already in the queue.
- Live Now Playing with album art and progress, pushed over SSE.
- Up Next: the same queue every guest sees.
- QR-code onboarding at `jukebox.home.arpa`.

**Host and administrator**

- Authenticated dashboard: playback control, queue management, health,
  statistics, logs, read-only configuration and library information.
- Playback supervisor that restarts mpv and reconnects after failures.
- Runtime state in a separate database, so restarts do not lose the queue.

**Offline tooling**

- A Library Builder that extracts metadata with Mutagen, repairs it with
  MusicBrainz, generates artwork files, validates and publishes atomically, and
  then exits.
- Build and validation reports you can read after the fact.

## Architecture overview

```text
        music files
             │
             ▼
     ┌───────────────┐   publishes atomically
     │ Library       │────────────────────────┐
     │ Builder       │                        ▼
     └───────────────┘                 library.db (immutable, FTS5)
        apps/builder                          │
                                              ▼
                                     ┌─────────────────┐   read/write
                                     │  Encore Server  │──────────────┐
                                     │                 │              ▼
                                     │ Search/Library  │         runtime.db
                                     │ Queue (FIFO)    │      (queue, history,
                                     │ Playback (mpv)  │       stats, admin)
                                     │ Event Bus       │
                                     │ HTTP/HTMX/SSE   │
                                     └─────────────────┘
                                        │        │
                                        ▼        ▼
                                    speakers   browsers
```

Two applications, one contract: the Builder produces `library.db` and never
touches the server's state; the Server reads that file read-only and keeps every
mutable fact in `runtime.db`.

Inside the server, Clean Architecture with dependencies pointing inward
(SAPRS 15.2):

```text
Presentation → Application → Domain → Repositories → Infrastructure
```

Services never call each other directly — they publish immutable events on an
in-process Event Bus (`SongQueued`, `SongStarted`, `SongFinished`,
`QueueAdvanced`, `PlaybackRecovered`, `LibraryReloaded`, `BuildCompleted`,
`HealthChanged`). Those rules are not aspirational:
[`tests/unit/test_architecture_guardrails.py`](tests/unit/test_architecture_guardrails.py)
fails the build when they are broken.

**Stack** — Python 3.12+, FastAPI, Jinja2, HTMX, Tailwind, SSE, SQLite + FTS5,
SQLAlchemy 2.x, Pydantic, mpv JSON IPC, systemd. Chosen and justified in
[ADR-002](docs/adr/ADR-002-htmx-instead-of-spa.md),
[ADR-004](docs/adr/ADR-004-internal-event-bus.md),
[ADR-005](docs/adr/ADR-005-mpv-playback-engine.md) and, for storage, ADR-003 as
superseded by
[ADR-009](docs/adr/ADR-009-split-the-storage-access-layer-by-mutability.md).

## Installation

> Not installable yet — the one-command `encore-install` is step 15. Everything it
> would run exists: the Builder produces a library, and the server starts and
> serves guests, an API and the live stream (steps 1–13). The commands below are
> the manual path, which is authoritative (SAPRS 13.2) and is what the installer
> will automate.

Target: Raspberry Pi 4, Raspberry Pi OS 64-bit (Debian 12 and Ubuntu 24.04 also
supported).

```bash
# 1. Build the library on a workstation
git clone https://github.com/clydepro/encore.git && cd encore
uv run python -m apps.builder.main --source /path/to/music --output ./dist-library

# 2. Install the appliance
curl -fsSL https://encore.example/install.sh | sudo bash   # automated
# ...or follow docs/Administrator-Guide.md step by step   # manual, authoritative

# 3. Start it
sudo systemctl enable --now encore
```

Filesystem layout (SAPRS 13.3):

```text
/opt/encore/application     the code
/opt/encore/venv            dependencies
/etc/encore/config.yaml     installation-time configuration (never rewritten)
/var/lib/encore/library.db  immutable library
/var/lib/encore/runtime.db  queue, history, statistics
/var/lib/encore/artwork/    cached artwork
/var/cache/encore/temp/     scratch space
```

## Development

Requirements: git and [`uv`](https://docs.astral.sh/uv/). `uv` fetches the
pinned Python; you never install Python by hand.

```bash
git clone https://github.com/clydepro/encore.git
cd encore
scripts/bootstrap.sh   # uv sync (editable install) + pre-commit hooks
scripts/check.sh       # ruff, formatter, yamllint, mypy, pytest, coverage
scripts/test.sh unit   # one suite
```

Quality gates are enforced locally on commit and again in CI
([five workflows](docs/Developer/Continuous-Integration.md)): Validate, Test,
Documentation, Security, Release.

The core is in place — domain model, Event Bus, configuration, both databases,
the Library Builder, search, playback, the queue — and there is now something to
start: `scripts/run-server.sh` serves the guest pages, the JSON API and the SSE
stream over the composed graph, so `scripts/check.sh` runs 1109 tests against
real code (1146 with the scheduled performance and Party Simulation suites, on a
machine that has mpv installed). The administrative interface is the remaining
half of the HTTP surface, and arrives with milestone 14.

Layout:

```text
.github/     CI, issue/PR templates, labels, dependabot
apps/        builder/ and server/ entry points
encore/      the package: api, config, controllers, domain, events, playback,
             repositories, search, services, templates, static, utilities
tests/       unit, integration, regression, performance, party_simulation
docs/        SAPRS, AIG, AEP, PBK, adr/, Developer/, api/, images/
ai/          current phase, handoff notes, prompts, context bundles,
             task templates, reviews, checklists
scripts/     bootstrap, lint, format, test, check, run-server, run-builder
tools/       developer-only utilities
```

## Documentation map

| I want to… | Read |
| ---------- | ---- |
| Understand the architecture | [SAPRS](docs/SAPRS/Encore-SAPRS.md) |
| Know what to build, in what order | [AI Implementation Guide](docs/AIG/AIImplementationGuide_AIG.md) |
| Know how changes are made here | [AI Engineering Playbook](docs/AEP/AIEngineeringPlaybook_AEP.md) |
| Know why a decision was made | [ADRs](docs/adr/README.md) |
| Set up my machine | [Getting started](docs/Developer/Getting-Started.md) |
| Use the jukebox as a guest | [User guide](docs/User-Guide.md) |
| Install or maintain the box | [Administrator guide](docs/Administrator-Guide.md) |
| Fix something that broke | [Troubleshooting](docs/Troubleshooting.md) |
| Call the API | [API reference](docs/api/README.md) |
| Ship a release | [Release process](docs/Developer/Release-Process.md) |
| Contribute | [CONTRIBUTING.md](CONTRIBUTING.md) |

The full map, including precedence between documents, is
[docs/README.md](docs/README.md).

## Roadmap

Implementation order is fixed by AIG Chapter 21; the PBK recommendation groups
the same work into ten milestones.

1. **Repository initialization. Done.**
2. **Core domain model. Done** — `encore/domain/`.
3. **Event Bus. Done** — `encore/events/`.
4. **Configuration. Done** — `encore/config/`, with structured logging.
5. **Repositories. Done** — `encore/repositories/`.
6. **Library Builder. Done** — `apps/builder/`.
7. **Search. Done** — `encore/search/`, over FTS5, benchmarked.
8. **Playback. Done** — `encore/playback/`, mpv IPC, supervisor, recovery.
9. **Queue. Done** — `encore/services/queue_service.py`, strict FIFO on `runtime.db`.
10. **Runtime database. Done** — migrations and repositories in step 5.
11. **FastAPI. Done** — `encore/api/`, `apps/server/`, versioned JSON API.
12. **HTMX. Done** — `encore/controllers/`, `encore/templates/`, server-rendered fragments.
13. **SSE. Done** — `/events`, `encore/services/sse_publisher.py`.
14. Administrative interface. ← next
15. Installer.
16. Party Simulation.
17. Documentation → `1.0.0`.

What exists and what is scaffolding is kept current in
[`ai/context/milestones.md`](ai/context/milestones.md), and what the last session
did and left is in [`ai/current-phase.md`](ai/current-phase.md) and
[`ai/HANDOFF.md`](ai/HANDOFF.md). This list is the order, not the status board.

Work in flight is tracked in the
[issues](https://github.com/clydepro/encore/issues) with the area labels from
[PBK Chapter 10](docs/ProjectBootstrapKit_PBK.md).

## License

MIT — see [LICENSE](LICENSE). SAPRS 15.15 lists MIT, BSD 3-Clause and Apache 2.0
as candidates for a permissive licence chosen before public release; MIT is used
here as the most permissive of the three and is trivially changed if the
maintainers prefer another. Audio files, album artwork and any user library are
never part of the repository or its license.

## Contributing

Start with [CONTRIBUTING.md](CONTRIBUTING.md), pick an issue labelled
[`good-first-issue`](https://github.com/clydepro/encore/labels/good-first-issue)
or open a [feature request](.github/ISSUE_TEMPLATE/feature_request.yml), and
follow the [Code of Conduct](CODE_OF_CONDUCT.md). AI-assisted contributions are
welcome and expected here; they must follow the
[Engineering Playbook](docs/AEP/AIEngineeringPlaybook_AEP.md) and disclose the
assistance in the pull request.

Security issues: see [SECURITY.md](SECURITY.md).
