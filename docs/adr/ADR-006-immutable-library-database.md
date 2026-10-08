# ADR-006: Immutable Library Database

## Status

Accepted

## Date

2026-10-07

## Context

A party is in progress. The queue has entries, mpv is mid-track, forty phones
are subscribed to one event stream — and somebody plugs in a USB drive of new
albums. If the running appliance could write into the canonical catalogue, the
library would become a third kind of state: part build output, part runtime
state, with all the concurrency and recovery questions that implies.

SAPRS 5.2 requires the runtime to open `library.db` read-only and never to
INSERT, UPDATE, DELETE or alter its schema.

## Decision

`library.db` is an **immutable build artifact**.

- The Builder writes to a temporary file, validates it, and publishes it
  atomically by rename into place; a partially constructed library never
  becomes the active one (SAPRS 5.8).
- The Server opens it read-only (URI `mode=ro`, `immutable` where the platform
  supports it) and owns no write path to it.
- Replacing the library means publishing a new file and signalling
  `LibraryReloaded`; services swap to the new handle at a safe point, not
  mid-command.
- Anything mutable — queue, history, statistics, administrative state — belongs
  in `runtime.db` (ADR-003).
- The publication contract is versioned inside the database so the Server can
  refuse an incompatible build with a clear diagnostic instead of failing later.

## Consequences

Positive:

- Catalogue reads need no locks against writers, which is what keeps search
  inside its 100 ms budget under party load.
- A failed build cannot corrupt a party: the previous library stays in place.
- Rollback is a file rename, and the library file itself is a backup unit.

Costs:

- Adding music requires a rebuild step; the "rescan" affordance must be honest
  about that.
- Library swaps need a defined safe point and a reference-counted handle.
- Two databases must be kept in step; the health endpoint reports both versions.

## Alternatives considered

- **Runtime import on scan.** Rejected: mixes build cost into the playback path
  and makes an interrupted import a corruption event.
- **One database with a `state` column separating concerns.** Rejected: violates
  SAPRS 5.1 and gives mutable and immutable data the same failure model.
- **Copy-on-write library into a runtime scratch file.** Rejected: doubles
  storage on a Pi SD card to buy nothing that a rename does not already buy.
- **Content-addressed media store with sidecar manifests.** Rejected: more
  machinery than a 15,000-row relational catalogue needs.
