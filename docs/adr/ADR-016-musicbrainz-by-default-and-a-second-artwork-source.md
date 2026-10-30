# ADR-016: MusicBrainz By Default, and a Second Source for Artwork

## Status

Accepted

## Date

2026-10-30

## Context

Issue #52 left a sentence in the Builder's report that nobody had read out loud: `MusicBrainz:
disabled`. It was on every build, including the ones an operator believed were helping.

Three facts made it inevitable, and the appliance measured all three:

- **Enrichment had no configuration home.** `--musicbrainz` was a command-line flag and nothing
  else — no key in `config.yaml`, nothing a systemd timer could see. A feature whose only switch is
  typed in a terminal is off in every place Encore actually runs. The Builder read the same file the
  Server reads, so the obvious fix had a snag: `extra="forbid"` (SAPRS 12.5) means an unmodelled
  section is a startup failure, and a valid `builder:` key would have stopped an appliance booting
  over a correct configuration file.
- **"Present" was being read as "correct."** SAPRS 6.6's second level says *fill gaps or correct
  implausible values*, and the stage implemented the first clause. An artist of `01` — a rip tool
  writing the track number into the artist field, 34 of 3,049 files — is present, so it was
  believed, so nothing downstream could tell that sixty files had been grouped under an artist named
  "01". Two files carried `date: 24`, and the album page printed 24.
- **SAPRS 6.7 named one source of artwork and stopped.** "Prefer embedded artwork" is the right rule
  for the 98% of M4A in the corpus that arrived from a rip with a picture in it, and it is the whole
  rule the Builder had. FLAC carries no picture at all, so a library of it published 463 grey tiles
  out of 703 albums, 1,989 songs of 3,049 with nothing pointing at an image, and reported nothing
  unusual — validation is about rows and references, and a missing picture is legal.

There was a fourth thing, smaller, which is #52's shape wearing a different coat:
`albums.musicbrainz_release_id` existed in the schema and was never written, and
`PreviousEnrichment.release_id` existed in a dataclass, was documented ("so a resumed build looks the
same recording up instead of searching for it again") and was never loaded.

## Decision

### 1. The Builder's policy lives in `config.yaml`, in a section the Server validates and never reads

`BuilderConfig` is added to `encore/config/models.py`, not to `apps/builder/`. The file is shared
(ADR-010), the model is shared, and one definition of a valid `config.yaml` is worth more than two
programs that each half-know it. Two values, both on:

```yaml
builder:
  musicbrainz: true
  album_art: true
```

Neither is consulted by a request handler, and ADR-001's boundary — nothing in `encore/` opens a
socket — is enforced by `test_the_builder_reaches_the_network_from_one_module_only`, not by a
default. This is the decision that makes "default-on" a property of the machine rather than of
whichever terminal ran the last build.

### 2. Three states on the command line, two in the file, and one that outranks both

`--musicbrainz` / `--no-musicbrainz` and `--album-art` / `--no-album-art` use
`argparse.BooleanOptionalAction`, whose default is `None` — *not given*, which is the case
configuration exists for. A flag that defaults to `False` would silently switch the file off on
every build that did not mention it, which is the same bug in a costume.

`--offline` is different in kind: it is a statement about this afternoon's network, not about the
library, so it forces both to off and is deliberately not expressible in the file.

### 3. Asking nothing is allowed to be free; forgetting is not

The reuse path now runs whether or not the asking runs. `pipeline._client()` hands the stages a
`DisabledClient` when enrichment is off, and the stage still backfills a gap from the previous
build's answers. Before this, an offline rebuild of unchanged files rewrote enriched values back to
the tags — SAPRS 6.6's "shall not be degraded to un-enriched data", broken by the flag that was
supposed to be the careful option.

### 4. Plausibility is a normalization rule, so it works with the network off

`is_usable` and `has_plausible_year` join `is_placeholder` in `apps/builder/normalization.py`, and
`NORMALIZATION_RULES_VERSION` goes to 2.

Two consequences worth having stated. A counter in an artist field falls to ADR-010's level 3 — the
directory the file was found in — so the appliance's own layout corrects a tagger's mistake with no
requests at all, and MusicBrainz is the second answer rather than the only one. And the version bump
means the first build after this upgrade re-reads every file, which is exactly what ADR-010's
"a normalization fix is a full rebuild" buys: the old conclusions were wrong, and invalidating them
is the fix rather than the cost.

The rule is narrow on purpose. A *counter* is a field that is nothing but a small integer; `21
Savage` and `07th Expansion` are names, and a value with a letter in it is level 1 evidence that
level 2 has no business overruling. The rule applies to artist fields and not to album titles,
because "21", "25", "30" and "13" are albums and there is no number a title cannot be. When nothing
better than the counter exists, `_resolve`'s last resort stays the tag itself: an implausible value
is questioned, never discarded, and a band named 1776 keeps the name.

A related policy, and the one that decides what gets *asked*: a counter is corrected, not searched
with. `artist:01 AND recording:"Morning"` matches a hundred releases and the client takes the first;
the query drops the artist and keeps the album, and refuses to ask at all when a bare track title is
all that would be left.

### 5. Artwork has a second precedence level, and it is exact or silent

SAPRS 6.7's list is a precedence, and this appends to it rather than contradicting it: the image in
the file, then the release's own cover from the Cover Art Archive, then nothing.

An album is asked about only when it has no picture anywhere in its group — including a reference
carried over by #52, which is a picture and not a hole — and only answered when the release can be
named by both things this library already knows: artist and album title, each equal after folding.
Not closest, not highest-ranked, not "the only result". A wrong cover is worse than a grey tile: a
guest scanning tiles for an album they cannot find is annoyed, and a guest looking at artwork from
someone else's record has been lied to about where something is, by the one part of the interface
they trusted to be a picture. That is ADR-010's argument about a guessed artist, applied to an
image.

The unit of asking is the album, by `Track.group_key` — the same rule `construction` files albums
by, deliberately shared because two copies of it would disagree about exactly the albums that
matter. 463 uncovered albums cost 926 requests rather than 2,000, and an identifier found on the way
is written onto the album so the next build asks once.

### 6. One module holds the socket

`apps/builder/albumart.py` is policy over the `MusicBrainzClient` protocol, and
`apps/builder/musicbrainz.py` remains the only file in Encore that opens one, for two hosts now. The
architecture test that says so is new with this decision, because #57 is the change that would
otherwise have made the rule a habit.

## Consequences

**Cost, stated where the switch is.** One request per second (MusicBrainz's published policy,
inherited by the archive), so a build with gaps takes about a minute per hundred files and roughly
two requests per album that has never been identified. A rebuild where nothing changed asks almost
nothing, because both #52's references and this decision's identifiers are read first. The
`max_requests` cap still bounds the worst case, and a build that hits it says so in the report
rather than taking the evening off quietly.

**The appliance now talks to a public service by default.** That is a change in behaviour on
upgrade, and it is documented in the Administrator Guide beside the two keys that turn it off. The
requests carry the corpus's own artist and album names, which is what a MusicBrainz lookup *is* — a
machine that does not want that conversation sets `musicbrainz: false` or `--offline` and loses
nothing it already had, per decision 3.

**Attribution is owed and is not yet rendered.** The Cover Art Archive asks that images be credited
to it and to the release they came from. The identifier is in the artifact —
`albums.musicbrainz_release_id`, which decision 4 made real — and the guest-facing credit line is
not, because an image credit is a UI decision with its own space and timing. It is named here so the
debt has an owner rather than an implication.

**A stale identifier is a decision this accepts.** A release id found once is reused until the file's
tags change, which is the cache working. If the identification was wrong, the fix is the retag or
`--full`, and `--full` is the documented way to say "ask me again".

**A cover is data, and is treated like the rest.** It goes through the same normalization, the same
content-addressed cache, the same `--max-artwork` bound on *new* images, and the same rule that a
picture that will not decode is a missing picture and never a missing track.

## Alternatives considered

**Leave it on the flag.** Rejected: that is the state this issue measured, and it fails in the
direction nobody notices — a build that reports "disabled" while an operator believes the appliance
repairs its own metadata.

**Prompt during installation.** Rejected, and against ADR-008: an appliance has no operator at
install time in the sense a prompt means, `encore-install` runs unattended in CI and in images, and
a decision made once in a terminal is invisible to the timer that rebuilds at four in the morning.

**Opt-in defaults with the documentation saying "consider enabling".** Rejected for the same reason
as the flag, plus one: the measured corpus is 99% tagged, and a feature that only experts turn on
cannot be what makes an ordinary library good.

**Accept the best-scoring release, or the only one returned.** Rejected in decision 5. The ranking
is a search engine's opinion, and an opinion about a substring is how a live album gets the sleeve
of the studio record. A tile that is grey is a fact; a tile that is wrong is a lie about where
something is.

**Search per track for covers.** Rejected: 1,989 requests instead of 926 for the same 463 answers,
and it files one album's picture under whichever track happened to be read first.

**Store the fetched images in `library.db`.** Rejected: SAPRS 5.6 forbids BLOBs in the artifact, the
cache already exists, is content-addressed, and is what an incremental build carries over.

**A Builder-only configuration file.** Rejected: two files that both claim to describe the
appliance is how an upgrade ends with one of them stale, and ADR-010 already made the Builder read
the Server's `paths` for the same reason.

**Leave `NORMALIZATION_RULES_VERSION` at 1 and let incremental builds keep their conclusions.**
Rejected: that is the bug, cached. ADR-010 priced this exact trade and the price is one rebuild.
