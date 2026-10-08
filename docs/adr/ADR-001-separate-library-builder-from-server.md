# ADR-001: Separate Library Builder from Server

## Status

Accepted

## Date

2026-10-07

## Context

Encore must serve a party from a Raspberry Pi 4 while also ingesting roughly
15,000 tracks of personal media. Scanning, tag extraction, MusicBrainz
lookup and artwork processing are bursty, network-dependent and
CPU-heavy. Playback is the opposite: continuous, latency-sensitive and it must
never stutter because something else on the box decided to do work.

SAPRS 2.1 states the two responsibilities belong to two applications. The
appliance also has to survive a partially completed build (SAPRS 5.8), which is
only tractable if building is a distinct, terminating activity.

## Decision

Ship two independent applications from one repository:

- `apps/builder` — an offline, terminating process that discovers music,
  normalises metadata, enriches it with MusicBrainz, generates artwork,
  constructs and validates `library.db`, publishes it atomically and exits.
- `apps/server` — the long-running appliance that opens the published library
  read-only and provides HTTP/HTMX/SSE, queue and playback.

The Builder never imports runtime playback components; the Server never imports
the Builder. Their only contract is the published `library.db` file plus the
artwork cache. AIG Chapter 4 and the guardrail tests make the boundary
executable.

## Consequences

Positive:

- Heavy library work cannot disturb audio output.
- Each application can be built, tested and versioned against its own risks.
- The appliance never needs MusicBrainz network access at runtime.

Costs:

- Two entry points and two dependency surfaces to keep honest.
- Library changes require an explicit rebuild-and-publish step rather than an
  in-place import.
- Users must understand "build then deploy", which the installer and
  documentation must make obvious.

## Alternatives considered

- **Single application with a background import job.** Rejected: couples
  metadata churn to playback latency and makes failure isolation (SAPRS 11.9)
  much harder.
- **Import into a live, mutable database on first scan.** Rejected: an
  interrupted import leaves the appliance with a half-known library, and the
  runtime would need to tolerate writes to the canonical data.
- **Separate repositories for Builder and Server.** Rejected: version skew
  between the two halves of one appliance is a support burden with no benefit.
