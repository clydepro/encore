# ADR-010: Library Builder Pipeline Shape and Library Ownership

## Status

Accepted

## Date

2026-10-09

## Context

ADR-001 made the Builder a separate, terminating application and ADR-006 made its
output an immutable artifact. Neither says how the Builder is built, and SAPRS
Chapter 6 spreads requirements across 6.3–6.12 that pull against each other: repair
metadata with MusicBrainz while an outage "must not prevent processing otherwise
valid music" (6.6); validate before publication (6.10) while never deleting user
music (6.8); skip work on unchanged files (6.9) while producing a complete, verified
database every run (5.9).

The corpus was measured rather than assumed — `/opt/music`, 3,049 playable files,
336 artist directories, 525 artist/album pairs:

- Tags are unusually complete: title and artist 99%, album 97%, track 95%, date 92%.
  **`albumartist` is on only 19%**, so the album-grouping field SAPRS 6.4 names must
  be derived, not read.
- **A path is not a reliable artist.** `country_5/`, `fun_songs/`, `gospel/` and
  `Unknown_Artist/` hold 44 files whose directory is a mood bucket (Deanna Carter,
  Elvis Presley, The Muppets appear only in tags) while `AC_DC/` and 330 others are
  genuine artist trees. Of the 15 files that have no usable tag artist and so would
  fall back to the path, **the directory name helps in 0 and the filename in 8**.
- **Tags are not automatically the truth either.** `Uncle Kraker - Drift Away.mp3` is
  tagged `Uncle Kracker` (correct) with album `No Stranger To Shame-ADVANCE` (a promo
  artifact), and a 53-minute single MP3 of a whole album is tagged
  `artist="AlbumWrap - Kenny Chesney"`.
- 15 playable files have no usable artist and 7 no usable title either. `Metadata`
  rejects both (SAPRS 6.5), so an unguarded Builder raises mid-run, after extraction
  and enrichment, with a traceback for what is an inventory fact.
- 231 files are real music in unsupported containers: 188 `.m4p` (DRM, so permanently
  unplayable rather than awaiting a codec), 41 `.wma`, 2 `.aif`.
- Artwork is embedded on 98% of M4A, 23% of MP3, 0% of FLAC. The artwork stage must
  treat "nothing to write" as normal (6.7).

## Decision

**The Builder is a staged pipeline whose only durable outputs are `library.db` and
the artwork cache, and which owns the library schema outright.**

**Structure.** `apps/builder/` holds one module per SAPRS 6.2 stage — discovery,
extraction, normalization, enrichment, artwork, duplicate analysis, construction,
search optimization, validation, publication. Stages are functions over immutable
value objects; stage boundaries are the only checkpoint positions (6.9). The Builder
imports `encore.domain` and `encore.utilities` and nothing from `encore.playback` or
`encore.controllers` (ADR-001, AIG 4). The guardrail for that rule is
`test_builder_does_not_import_runtime_playback`, which iterates
`apps/builder/**/*.py` — a tree containing no Python today, so the test passes
vacuously and starts enforcing on the first commit that puts code there. Worth
stating, because the alternative is trusting a green check that checks nothing.

**Schema ownership.** The canonical DDL for `artists`, `albums`, `songs`, `artwork`,
`music_files` and every derived search structure (5.3, 5.4) lives in the Builder and
nowhere else; the Server holds `SELECT` statements only (ADR-009). A schema
duplicated into the read side is a schema that can drift there unnoticed.
`PRAGMA integrity_check` and `foreign_key_check` run inside the Builder's publication
gate, not as a Server startup convenience.

**Writing.** `library.db` is built with raw `sqlite3` in `paths.temp_dir` and
published by atomic rename to `paths.library_db` (5.8) — never in place. The write
path lives in `apps/builder/`, not `encore/repositories/`: putting Builder inserts in
the package the Server imports for reads would reintroduce the coupling ADR-001
exists to prevent. One shared column-name module is fine — that is data, not
behaviour.

**Metadata precedence is fixed, not emergent.**

1. An embedded tag, when present and non-empty (6.4).
2. MusicBrainz, to fill a gap or correct a value that fails normalization. Each
   override recorded per field as `repaired` in the report (6.12).
3. The path, as a hint only, and only where the top directory is an artist tree
   rather than a collection bucket. Never when a tag exists to contradict it.
4. The filename, as a last resort for artist or title, and only for unambiguous
   patterns: `NN_Title.ext`, or `Artist - Title` as used throughout `country_5/`,
   `fun_songs/` and `gospel/`. A guessed artist is worse than an absent one, because
   it silently groups songs under the wrong name.

**Normalization happens once, in its own stage** (6.5), producing the `normalized_*`
and `sort_*` fields the domain already carries: whitespace collapsed, case folded for
comparison, `Various Artists`/`Unknown` recognized as placeholders, tool prefixes such
as `"AlbumWrap - "` stripped, promo markers (`-ADVANCE`, `[Clean_Version]`) removed
with the original retained, track and disc numbering canonicalized across `5`, `5/12`
and `05`, and album grouping keyed on album artist where present else primary artist.

**Files that cannot be catalogued are a reported outcome, not a crash.** Discovery
accepts a file only if `AudioFormat.for_path` returns a format (6.3). A file whose
artist or title survives none of the four levels is recorded as skipped with a reason
and excluded from `songs`; the run continues and may still publish. 6.10's required-
fields validation applies to rows that were *built*. The alternative is one mystery
file on a 20 GB drive making the library unbuildable. The Builder never modifies,
moves or deletes a media file (6.8); a rejected song is simply absent, so the report
must print its final count beside a `find` count.

Unplayable containers are counted in aggregate — `231 files skipped: 188 .m4p (DRM),
41 .wma, 2 .aif` — not itemized, because a 263-line warning block trains the operator
to ignore the report.

**Incremental builds key on `(path, size, mtime_ns, embedded-tag hash)`** plus the
normalization-rule version, so fixing a rule invalidates rather than silently
reuses. A changed file is rebuilt even when MusicBrainz is unreachable, using tags
plus the previous build's enrichment, never degraded to un-enriched data (6.6, 6.9).

**Music root.** `paths.music_dir`, default **`/opt/music`**, read by the Builder only;
the Server never scans the filesystem (12.2's "Library location"). Absolute for the
same reason as the other paths — systemd provides no working directory. User music
belongs under `/opt`; `/var/lib/encore` is for Encore's own artifacts. It must land in
`PathsConfig`, `examples/config.yaml` and the Administrator guide in one change,
because `extra="forbid"` plus the test that executes the example makes any two of the
three fail CI.

## Consequences

Positive:

- One run against the real corpus produces a complete, verifiable description of it —
  including its 15 unidentifiable and 231 unplayable files — without a traceback.
- Precedence and normalization decided before coding means the corpus ambiguity is
  resolved by a rule and a test, not by whichever code was written first.
- Stage boundaries make the expensive parts resumable, which a 20 GB build wants and
  a 15,000-song build will need.
- A schema that exists in one place cannot disagree with itself.

Costs:

- Four-level precedence is harder to debug than "tags win". The per-field `repaired`
  record is therefore not optional; it is the only way to answer "why is this song
  under this artist?".
- A skipped song is indistinguishable from a song that was never there, unless the
  report's counts are compared.
- The Builder is the only component that can break the Server's queries. ADR-009's
  contract test — every SELECT resolves against a real built database — is what makes
  that loud rather than surprising.
- Rule-versioned caching means a normalization fix is a full rebuild.
- At 15,000 songs the FTS5 index and artwork cache are roughly four times the measured
  size. The budget is asserted in `tests/performance/` against a synthetic scale
  fixture until the library is big enough to measure directly.

## Alternatives considered

- **Schema defined in a module both applications import.** Rejected: sounds DRY, is
  the coupling ADR-001 forbids — the runtime would depend on Builder code at import
  time, and a build-only column becomes runtime-visible.
- **Path authoritative when tags are absent or conflicting.** Rejected on the
  evidence: the corpus has two mutually incompatible layouts, and the path rescues
  0 of the 15 files that would need it. A rule that is consulted only when it is
  wrong is worse than no rule, because the 3,034 files it never touches make it look
  verified.
- **Fail the build on any uncatatalogueable file.** Rejected: 6.8 forbids the Builder
  acting on user music, and aborting because 15 of 3,049 files are unidentifiable
  hands a broken appliance to a party.
- **Normalize during extraction.** Rejected: extraction is per-format, so rules would
  be re-derived in each reader. 6.2 separates the stages for this reason.
- **MusicBrainz as primary source, tags as fallback.** Rejected: 6.4 makes tags
  primary; lookups would dominate a 3,049-file build with network latency, an outage
  becomes a build failure contrary to 6.6, and these tags are better than a blind
  match.
- **Store artwork as BLOBs in `library.db`.** Rejected: 5.6 prefers files with
  references; embedding inflates every library copy, defeats content-addressed
  caching, and slows the artifact the Server opens on every start.
- **Watch the filesystem and rebuild online.** Rejected by ADR-001 and 5.2: runtime
  import makes an interrupted scan a corruption event on the authoritative catalogue.
