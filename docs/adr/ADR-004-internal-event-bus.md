# ADR-004: Internal Event Bus

## Status

Accepted

## Date

2026-10-07

## Context

Playback, queue, search, SSE, health and statistics all need to know facts
about each other: a song started, a song finished, the queue advanced, mpv
recovered, the library was reloaded, health changed. Wiring those needs
directly produces a graph of dependencies in which every service knows several
others — the fastest route to the coupling that AIG Chapter 4 forbids, and to
the failure cascade SAPRS 11.9 explicitly rules out.

## Decision

Implement an **in-process, in-memory Event Bus** as the only cross-service
notification mechanism.

- Events are immutable, typed, timestamped dataclasses defined in
  `encore/events/`. An event represents a fact that already happened.
- Publishers never reference subscribers and subscribers never reference
  publishers (AIG 8).
- The initial event set is fixed: `SongQueued`, `SongStarted`, `SongFinished`,
  `QueueAdvanced`, `PlaybackRecovered`, `LibraryReloaded`, `BuildCompleted`,
  `HealthChanged`.
- Handlers are isolated: one subscriber raising an exception is logged and
  cannot prevent other subscribers from receiving the event (SAPRS 11.3).
- Long-running handlers run outside the playback path (SAPRS 11.4); playback
  timing never waits on a browser.
- The Bus is not a broker. It has no disk queue, no retry, no cross-process
  transport and no ordering promise beyond the stream that produced it.

## Consequences

Positive:

- Services keep single responsibilities and a shallow dependency graph.
- New consumers (statistics, history, a future webhook) subscribe instead of
  being woven into existing code paths.
- Tests can drive a whole scenario by publishing events, and event delivery is
  an explicit seam to assert on.

Costs:

- Debugging requires observability: structured logging must record event names,
  correlation and handler failures.
- Nothing survives a process restart; durable state stays in `runtime.db`, not
  in the Bus.
- An in-process bus tempts people to treat it as a message queue. It is not:
  delivery is best-effort within one process.

## Alternatives considered

- **Direct service-to-service calls.** Rejected: violates the guardrails and
  makes failure isolation accidental rather than designed.
- **Redis/RabbitMQ/MQTT.** Rejected: a network dependency and an operational
  surface for a single-process appliance (SAPRS 11.1 says so explicitly).
- **Python `blinker`/observer libraries.** Rejected: untyped signals, no
  isolation between handlers, and the isolation is the point.
- **`asyncio.Queue` per consumer.** Rejected as the public interface: fine as an
  implementation detail inside SSE publishing, but it leaks task ownership into
  every service.
- **An event log/database outbox.** Rejected for v1: history and statistics
  already persist the facts that matter durably.
