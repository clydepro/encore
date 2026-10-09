"""Where a value comes from when the tags do not say (SAPRS 6.4; ADR-010).

ADR-010 fixes the precedence rather than letting it emerge from whichever code
was written first:

1. An embedded tag, when present and non-empty.
2. MusicBrainz, to fill a gap or correct a value normalization rejects — applied
   in `apps.builder.enrichment`, which needs a network and a cache, so this module
   only marks the gap.
3. The path, as a hint, and only where the top directory is an artist tree rather
   than a collection bucket. Never when a tag exists to contradict it.
4. The filename, as a last resort, and only for unambiguous patterns.

Levels 3 and 4 are the interesting ones, because the corpus measurements in
ADR-010 say they are mostly useless and one of them is not: `country_5/`,
`fun_songs/`, `gospel/` and `Unknown_Artist/` hold 44 files whose directory is a
mood, not a person, and of the 15 files with no usable tag artist the directory
name supplies a usable artist in **zero** of them. The filename supplies one in 8.

So the directory is trusted only when it has *proved* itself an artist tree —
which is decided from the tags of the files inside it, not from a list of names.
That is the difference between a rule and a guess: `AC_DC/` is an artist tree
because the 214 files in it say "AC/DC", and `country_5/` is not because the files
in it say six different things. A directory with no tagged files at all is not an
artist tree either, because "a guessed artist is worse than an absent one" is the
sentence in the ADR that this function exists to obey.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final

from apps.builder.normalization import (
    collapse,
    fold,
    is_placeholder,
    parse_position,
    sort_key,
    strip_promo_markers,
    strip_tool_prefix,
)
from apps.builder.records import ExtractedFile, FieldProvenance, Track
from encore.repositories.contract import MetadataSource

__all__ = [
    "ArtistTrees",
    "filename_hint",
    "normalize_track",
    "path_hint",
    "tree_map",
]

#: A directory name's separators, collapsed to single spaces for comparison.
_SEPARATOR: Final = re.compile(r"[_\s]+")

#: What fraction of a directory's tagged files must agree on one artist before the
#: name is treated as evidence rather than coincidence. Deliberately not 1.0: one
#: mistagged file out of 214 is a mistag, and demanding perfection means the hint
#: is never available in exactly the large trees where it is most useful.
_AGREEMENT_RATIO: Final = 0.8

#: Minimum tagged files needed to judge a tree. One file agrees with itself, which
#: proves nothing about whether the directory is named after its artist.
_MINIMUM_EVIDENCE: Final = 3

#: `Artist - Title`, `Title`, `05 - Artist - Title`: the filename forms the corpus
#: actually uses. `ADR-010` names `NN_Title` and `Artist - Title`; both appear as
#: `NN - Artist - Title` in practice, so the number is peeled off first.
_SPACED: Final = re.compile(r"\s+-\s+")
_TRACK_PREFIX: Final = re.compile(r"^\d{1,3}[ ._-]+\d{1,3}\.?$|^\d{1,3}[ ._-]+", re.IGNORECASE)
_NUMERIC_ONLY: Final = re.compile(r"^\d{1,4}[ ._-]*$")
#: Two dash-separated parts are "Artist - Title" and nothing else can be. One is a
#: title; three is a coin toss, so the pattern is read only at this width.
UNAMBIGUOUS_SEGMENTS: Final = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtistTrees:
    """Which top-level directories are artist trees, and what they are trees of.

    Attributes:
        artists: Directory name → the artist its own files report, folded for
            comparison. A directory is absent when it is a bucket, when the files
            disagree, or when there is not enough evidence to tell.
        judged: Directories examined. In the report, so an operator can see that
            "not an artist tree" was a decision made from 3,049 files rather than
            a list someone typed.
    """

    artists: Mapping[str, str] = field(default_factory=dict)
    judged: int = 0

    def tree_for(self, directory: str) -> str | None:
        return self.artists.get(directory)

    def is_artist_tree(self, directory: str) -> bool:
        return directory in self.artists


def tree_map(extracted: Sequence[ExtractedFile], root: Path) -> ArtistTrees:
    """Classify every top-level directory under `root` from its own contents.

    Args:
        extracted: Everything discovery accepted, already probed.
        root: `paths.music_dir`, so a file directly in the root has no top
            directory and gets no hint — correct, since the root is a container,
            not an artist.

    Returns:
        The trees, judged once per build rather than once per file: the question
        "is `fun_songs/` an artist?" has one answer per run and asking it 44 times
        is how a rule drifts.
    """

    evidence: dict[str, Counter[str]] = {}
    for item in extracted:
        directory = _top_directory(item.file.path, root)
        if directory is None:
            continue
        artist = _tag_artist(item)
        if artist is None:
            evidence.setdefault(directory, Counter())
            continue
        evidence.setdefault(directory, Counter())[artist] += 1

    artists = {
        directory: winner
        for directory, counts in evidence.items()
        if (winner := _agreed_artist(counts)) is not None
    }
    return ArtistTrees(artists=MappingProxyType(artists), judged=len(evidence))


def path_hint(path: Path, root: Path, trees: ArtistTrees) -> str | None:
    """Level 3: the artist the directory implies, if it has earned the right.

    The name is compared against the tree's own evidence rather than formatted,
    so `AC_DC/` yields "AC/DC" — what the files say — and not "AC_DC", which is
    what a shell would have produced and what would then be a second artist.
    """

    directory = _top_directory(path, root)
    if directory is None:
        return None
    return trees.tree_for(directory)


def filename_hint(name: str) -> tuple[str | None, str | None]:
    """Level 4: `(artist, title)` from a filename, or as much of it as is certain.

    Recognises `Artist - Title`, `NN - Artist - Title`, `NN_Title` and a bare
    `Title`, which between them are the forms the corpus uses. A name with three
    or more ` - ` segments is left alone: `A - B - C` could be artist-title-album,
    artist-album-title or a title containing a dash, and ADR-010's ruling is that
    a guess here is worse than nothing.

    The extension is dropped without looking at it, because by this point
    discovery has already accepted the container.
    """

    stem = Path(name).stem
    stem = collapse(_TRACK_PREFIX.sub("", stem, count=1))
    if not stem or _NUMERIC_ONLY.match(stem):
        # `03.mp3` and `03 - 12.mp3` say where a track sits and nothing about what
        # it is. Cataloguing "03" as a title puts a row of numbers on the All Songs
        # page, and the skip ledger's "no usable title" is the truer line.
        return None, None
    segments = [collapse(part) for part in _SPACED.split(stem) if collapse(part)]
    if len(segments) == 1:
        return None, segments[0]
    if len(segments) == UNAMBIGUOUS_SEGMENTS:
        return segments[0], segments[1]
    return None, None


def normalize_track(
    extracted: ExtractedFile,
    *,
    root: Path,
    trees: ArtistTrees,
) -> Track:
    """Resolve one file into the track the library will hold.

    Tags first (SAPRS 6.4), then the directory where it has proved itself an artist
    tree, then the filename. Every field records where its value came from, which
    is ADR-010's answer to "why is this song under this artist?" and the only
    defence against a four-level rule becoming impossible to debug.
    """

    tags = extracted.tags
    hint = path_hint(extracted.file.path, root, trees)
    name_artist, name_title = filename_hint(extracted.file.path.name)

    title, title_source = _resolve(
        tags.get("title"),
        hint=None,
        fallback=name_title,
        kind="title",
    )
    track_artist, track_artist_source = _resolve(
        tags.get("artist"),
        hint=hint,
        fallback=name_artist,
        kind="artist",
    )
    album_artist, album_artist_source = _resolve(
        tags.get("album_artist"),
        hint=hint if track_artist_source != MetadataSource.TAG else None,
        fallback=None,
        kind="artist",
    )

    # ADR-010: grouping keys on album artist where present, else primary artist.
    # `track_artist` stays the performing artist, which is what SAPRS 4.4 puts on
    # a Song and what a compilation's track list must not overwrite.
    if album_artist:
        grouping, grouping_source = album_artist, album_artist_source
    else:
        grouping, grouping_source = track_artist, track_artist_source

    album, album_source = _resolve(tags.get("album"), hint=None, fallback=None, kind="album")

    provenance = {
        "title": _provenance("title", tags.get("title"), title, title_source),
        "artist": _provenance("artist", tags.get("artist"), grouping, grouping_source),
        "album": _provenance("album", tags.get("album"), album, album_source),
    }
    if track_artist and track_artist != grouping:
        provenance["album_artist"] = _provenance(
            "album_artist", tags.get("album_artist"), album_artist or grouping, album_artist_source
        )

    track_number, _ = _position(tags.get("track"))
    disc_number, _ = _position(tags.get("disc"))
    genre = _value(tags.get("genre"))
    date = _value(tags.get("date"))

    return Track(
        extracted=extracted,
        artist=grouping,
        album_artist=album_artist or None,
        track_artist=track_artist or None,
        album=album or None,
        title=title,
        normalized_artist=fold(grouping),
        normalized_album=fold(album) if album else "",
        normalized_title=fold(title),
        sort_artist=_sort(grouping),
        sort_album=_sort(album) if album else "",
        sort_title=_sort(title),
        track_number=track_number,
        disc_number=disc_number,
        genre=genre or None,
        date=date or None,
        musicbrainz_artist_id=_value(tags.get("musicbrainz_artist_id")) or None,
        musicbrainz_release_id=_value(tags.get("musicbrainz_release_id")) or None,
        musicbrainz_recording_id=_value(tags.get("musicbrainz_recording_id")) or None,
        provenance=provenance,
    )


def _resolve(
    tag: str | None,
    *,
    hint: str | None,
    fallback: str | None,
    kind: str,
) -> tuple[str, str]:
    """Apply levels 1, 3 and 4 to one field, in that order.

    A placeholder is absence with better manners: `Various Artists` is a present,
    non-empty tag value, and SAPRS 6.5's "empty values" rule means it must not win
    a precedence contest it should lose.
    """

    tagged = _value(tag)
    if tagged and not is_placeholder(tagged, kind=kind):
        return tagged, MetadataSource.TAG
    if hint and not is_placeholder(hint, kind=kind):
        return hint, MetadataSource.PATH
    guessed = _value(fallback)
    if guessed and not is_placeholder(guessed, kind=kind):
        return guessed, MetadataSource.FILENAME
    # Nothing usable. The tag value, if there was one, is returned anyway: a
    # track filed under "Various Artists" is at least honest about the source, and
    # the skip decision belongs to `Track.missing_fields`, not to this function.
    return tagged, MetadataSource.TAG if tagged else MetadataSource.CONSTANT


def _value(tag: str | None) -> str:
    """Display form: collapsed, tool watermark removed, promo suffix removed.

    Both cleanups are applied here rather than in `normalize_track` so that every
    field gets the same treatment, including the ones that never reach a
    `Normalized` result. `strip_promo_markers` keeps the original by returning
    `changed`, which the provenance record reads as a repair.
    """

    text = collapse(tag)
    if not text:
        return ""
    stripped = strip_promo_markers(strip_tool_prefix(text).value)
    return stripped.value


def _position(tag: str | None) -> tuple[int | None, int | None]:
    return parse_position(tag)


def _sort(value: str) -> str:
    return sort_key(value)


def _provenance(field_name: str, tag: str | None, effective: str, source: str) -> FieldProvenance:
    return FieldProvenance(
        field=field_name,
        original=collapse(tag) or None,
        effective=effective or None,
        source=source,
        repaired=source != MetadataSource.TAG or _changed_by_rules(tag, effective),
    )


def _changed_by_rules(tag: str | None, effective: str) -> bool:
    """Whether normalization altered the value, as opposed to displaying it.

    What a repair *is*: the file said one thing and the library stores another.
    Comparing the cleaned value against the effective one would report nothing, because
    they are the same string by construction; comparing against the folded one would
    miss `Beta [Clean_Version]` → `Beta`, since folding removes the bracket either way.
    The only comparison that answers the question is against the tag as it was written.
    """

    return bool(effective) and collapse(tag) != effective


def _tag_artist(item: ExtractedFile) -> str | None:
    """The artist this file's own tags name, folded, if it names one at all.

    Album artist first: in a directory of one artist's albums the track artist is
    frequently a feature credit, and the tree is named after the release artist.
    """

    for key in ("album_artist", "artist"):
        value = _value(item.tags.get(key))
        if value and not is_placeholder(value):
            return fold(value)
    return None


def _agreed_artist(counts: Counter[str]) -> str | None:
    """The artist a directory's files agree on, if they agree enough."""

    total = sum(counts.values())
    if total < _MINIMUM_EVIDENCE or not counts:
        return None
    winner, count = counts.most_common(1)[0]
    return winner if count / total >= _AGREEMENT_RATIO else None


def _top_directory(path: Path, root: Path) -> str | None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    parts = relative.parts[:-1]
    return collapse(parts[0]) if parts else None
