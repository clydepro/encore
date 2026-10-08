# apps/builder

Entry point for the **Encore Library Builder** — the offline, terminating
process described in SAPRS Chapter 6 and AIG Chapter 16.

## Responsibilities

- Discovers music files and extracts metadata (Mutagen).
- Repairs/normalises metadata out of process using MusicBrainz.
- Generates artwork files and the optimised `library.db` (FTS5 included).
- Validates the result and publishes it atomically.
- Emits a build report and a validation report, then exits.

## Status

Intentionally empty during the bootstrap phase (PBK 1). The builder entry point
(`apps/builder/main.py`) arrives with milestone 6 (Library Builder) in AIG
Chapter 21.

## Rules that bind this application

- It must never import runtime playback components (ADR-001, SAPRS 2.5).
- It must never touch `runtime.db` (SAPRS 5.1).
- A partially built database must never replace a published one (SAPRS 5.8).
