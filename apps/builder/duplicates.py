"""Stage 6 — duplicate analysis (SAPRS 6.8; ADR-010).

Informational, and deliberately so. SAPRS 6.8 says the Builder "must not silently
delete user music", so the output of this stage is a list in a report and nothing
else: no row is dropped, no file is touched, no song is merged. A library that
contained two copies of *Back in Black* because someone copied a folder twice
would still play both, and the operator learns about it from the report rather
than from a gap where their other copy used to be.

What counts as a duplicate is a size-and-duration match or a metadata match, not a
content hash. Hashing 20 GB to find out that the same album exists twice is the
kind of optimization AEP 14 tells us to measure first, and the answer is that a
file of the same length as another file, at the same duration, under the same
artist and album, with the same track number, is the same recording for the
purpose of a warning line.

The two are different findings and are reported separately, because they have
different causes: identical files come from a copy, and identical metadata on
*different* files comes from a bad rip or a re-download.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from apps.builder.records import Track

__all__ = ["Duplicate", "DuplicateReport", "analyse"]

#: A "duplicate" needs two of something. Named because the same number decides three
#: separate questions in this module, and a group of one is not a finding.
MINIMUM_GROUP: Final = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class Duplicate:
    """One group of files that look like the same thing.

    Attributes:
        paths: The files, sorted, at least two of them.
        kind: `"identical-file"` when size and duration match, `"same-recording"`
            when only the metadata does.
        label: What the group is, for the report line — `Artist — Album / Track`.
    """

    paths: tuple[Path, ...]
    kind: str
    label: str

    @property
    def count(self) -> int:
        return len(self.paths)

    def __str__(self) -> str:
        return f"{self.kind} x{self.count}: {self.label}"


@dataclass(frozen=True, slots=True, kw_only=True)
class DuplicateReport:
    """Everything the analysis found, in the shape the report wants it."""

    groups: tuple[Duplicate, ...] = ()

    @property
    def total(self) -> int:
        return sum(group.count for group in self.groups)

    def lines(self, limit: int = 20) -> tuple[str, ...]:
        """The top `limit` groups, then one honest line about the rest.

        ADR-010's rule about skip lists applies to duplicates too: a 200-line
        block teaches the operator to skip the section, so the list is capped and
        the remainder is counted.
        """

        ordered = sorted(self.groups, key=lambda group: (-group.count, group.label))
        listed = [str(group) for group in ordered[:limit]]
        if len(ordered) > limit:
            listed.append(f"... and {len(ordered) - limit} more duplicate groups")
        return tuple(listed)


def analyse(tracks: Sequence[Track]) -> DuplicateReport:
    """Group tracks that describe the same recording.

    Args:
        tracks: Catalogable tracks only. A skipped file cannot duplicate anything,
            because nothing was concluded about it.

    Returns:
        Groups with more than one member. Nothing is removed from `tracks` as a
        consequence of appearing here (SAPRS 6.8).
    """

    by_bytes: dict[tuple[int, int], list[Track]] = defaultdict(list)
    by_identity: dict[tuple[str, str, str, int | None], list[Track]] = defaultdict(list)
    for track in tracks:
        seconds = int(track.duration.total_seconds())
        by_bytes[(track.extracted.file.size_bytes, seconds)].append(track)
        by_identity[
            (
                track.normalized_artist,
                track.normalized_album,
                track.normalized_title,
                track.track_number,
            )
        ].append(track)

    groups: list[Duplicate] = []
    claimed: set[Path] = set()
    for matches in by_bytes.values():
        if len(matches) < MINIMUM_GROUP:
            continue
        paths = tuple(sorted((track.path for track in matches), key=str))
        groups.append(Duplicate(paths=paths, kind="identical-file", label=_label(matches[0])))
        claimed.update(paths)

    for matches in by_identity.values():
        if len(matches) < MINIMUM_GROUP:
            continue
        fresh = tuple(sorted((t.path for t in matches if t.path not in claimed), key=str))
        if len(fresh) < MINIMUM_GROUP:
            continue
        groups.append(Duplicate(paths=fresh, kind="same-recording", label=_label(matches[0])))
        claimed.update(fresh)

    return DuplicateReport(groups=tuple(groups))


def _label(track: Track) -> str:
    artist = track.artist or "(no artist)"
    album = track.album or "(no album)"
    title = track.title or track.path.stem
    position = f" track {track.track_number}" if track.track_number else ""
    return f"{artist} — {album} / {title}{position}"
