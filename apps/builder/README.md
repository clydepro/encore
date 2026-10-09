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

Implemented (AIG 21 step 6, ADR-010). Run it as `encore-builder`, or
`uv run python -m apps.builder.main`; `scripts/run-builder.sh` is a wrapper that
takes the music directory as its first argument.

One module per stage, in the order the pipeline calls them: `discovery`,
`extraction`, `normalization`, `precedence`, `musicbrainz` + `enrichment`,
`artwork`, `duplicates`, `construction` + `schema` + `search_index`,
`validation`, `publication`. `pipeline.py` owns the ordering, the accounting and
the `BuildCompleted` event; `state.py` owns incremental reuse; `report.py` owns
what the operator reads.

It is the only program that writes `library.db`, and the only place its schema is
defined. The Server's read side lives in `encore/repositories/library/`, and the
shared naming that keeps the two honest is `encore/repositories/contract.py`.

## Rules that bind this application

- It must never import runtime playback components (ADR-001, SAPRS 2.5).
- It must never touch `runtime.db` (SAPRS 5.1).
- A partially built database must never replace a published one (SAPRS 5.8).
- It must never modify, move, rename or delete a media file (SAPRS 6.8). A file it
  cannot use is reported, not fixed on disk.
- Only `musicbrainz.py` may open a network connection, and only when enrichment is
  requested. A stage that needs the internet to finish a build has misunderstood
  SAPRS 6.6.
