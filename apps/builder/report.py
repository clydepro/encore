"""The build report (SAPRS 6.12; ADR-010's counting rules).

SAPRS 6.12 lists ten numbers a build must state. They are all here, and the way
they are presented is the part that decides whether anyone reads them.

ADR-010 sets the two rules that shape this module:

* **Unsupported files are counted in aggregate.** The corpus's 231 unplayable files
  become `231 skipped: 188 .m4p (DRM), 41 .wma, 2 .aif`, because a 263-line warning
  block trains the operator to ignore the report — and the ten lines that mattered
  would be in it. Paths still go to the detail file for the run.
* **A skipped song is indistinguishable from a song that was never there**, unless
  the counts are compared. So `found`, `skipped` and `catalogued` appear on one line
  and the arithmetic is asserted rather than narrated: if 3,312 files were scanned
  and 3,049 were catalogued, 263 went somewhere, and this report says where.

Text is the primary rendering because the Builder runs on a terminal, over SSH, at
11pm. The JSON is for the test suite and for `BuildCompleted`, and is generated from
the same fields rather than alongside them.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from apps.builder.records import SkippedFile, SkipReason

__all__ = ["BuildReport", "SkipLedger", "describe"]

#: How many duplicate groups the printed report names before counting the rest. ADR-010's
#: rule for skips, applied to the section that most resembles a TODO list.
DUPLICATE_LINES: Final = 20

#: Human label per aggregate skip key, so the report says what a suffix means
#: rather than making an operator look it up. DRM in particular is a permanent no,
#: not a codec to add later (ADR-010).
_REASONS: Final[Mapping[str, str]] = {
    SkipReason.UNSUPPORTED_FORMAT: "unsupported container",
    SkipReason.UNREADABLE: "could not be read",
    SkipReason.NO_ARTIST: "no usable artist in tags, path or filename",
    SkipReason.NO_TITLE: "no usable title in tags or filename",
    SkipReason.NO_DURATION: "no decodable duration",
    SkipReason.ALREADY_INDEXED: "another copy is already indexed",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class SkipLedger:
    """Every file the Builder did not catalogue, counted the way ADR-010 wants.

    Attributes:
        entries: The individual records, for the detail file and for JSON.
        aggregate: Count per `SkippedFile.aggregate_key`, which is what makes 188
            `.m4p` files one line rather than 188.
    """

    entries: Sequence[SkippedFile] = ()
    aggregate: Counter[str] = field(default_factory=Counter)

    @classmethod
    def of(cls, entries: Sequence[SkippedFile]) -> SkipLedger:
        return cls(
            entries=tuple(entries),
            aggregate=Counter(entry.aggregate_key for entry in entries),
        )

    @property
    def total(self) -> int:
        return len(self.entries)

    def lines(self) -> tuple[str, ...]:
        """One line per reason, sorted by how many files it accounts for."""

        grouped: dict[str, Counter[str]] = {}
        for key, count in self.aggregate.items():
            reason, _, detail = key.partition(":")
            grouped.setdefault(reason, Counter())[detail] += count
        lines: list[str] = []
        for reason, details in sorted(grouped.items(), key=lambda item: -sum(item[1].values())):
            total = sum(details.values())
            # Only the format breakdown is worth naming: ".m4p 188" is a fact about the
            # library, while "1 unreadable: PermissionError x 30" repeats the sentence
            # that precedes the colon and buries the one line the operator can act on.
            named = {name: count for name, count in details.items() if name}
            if not named:
                lines.append(f"{total:>6} {describe(reason)}")
                continue
            listed = ", ".join(
                f"{count} {name}"
                for name, count in sorted(named.items(), key=lambda item: (-item[1], item[0]))[:6]
            )
            remaining = len(named) - len(listed.split(", "))
            suffix = f", … {remaining} more" if remaining > 0 else ""
            lines.append(f"{total:>6} {describe(reason)}: {listed}{suffix}")
        return tuple(lines)


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildReport:
    """The ten numbers SAPRS 6.12 requires, plus what they mean.

    Attributes:
        discovered: Files found under `paths.music_dir` that Encore can play.
        scanned: Every file looked at, including the ones it cannot play — the
            number an operator will compare against `find | wc -l`, which is why it
            is in the report and not only in the log.
        processed: Files that became rows.
        skipped: Files that did not, with the ledger behind the count.
        repaired: Fields whose value came from something other than a tag
            (ADR-010's `repaired` record, summed).
        artwork: Images written to the cache this run. Zero is a normal value: an
            unchanged rebuild should write nothing.
        songs: Rows in `songs` after publication. Compared against `processed` by
            the validation stage, and printed beside `scanned` here.
        validation: The publication gate's outcome, one line per check.
    """

    discovered: int = 0
    scanned: int = 0
    processed: int = 0
    skipped: SkipLedger = field(default_factory=SkipLedger)
    repaired: int = 0
    artwork: int = 0
    songs: int = 0
    artists: int = 0
    albums: int = 0
    duplicates: Sequence[str] = ()
    warnings: Sequence[str] = ()
    errors: Sequence[str] = ()
    validation: Sequence[tuple[str, str, str]] = ()
    duration_seconds: float = 0.0
    published_to: Path | None = None
    library_version: str = ""
    incremental: bool = False
    reused: int = 0
    musicbrainz: str = "disabled"

    @property
    def skip_count(self) -> int:
        return self.skipped.total

    @property
    def warning_count(self) -> int:
        return len(self.warnings) + len(self.skipped.entries)

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def validated(self) -> bool:
        """True when every validation check passed or warned without failing."""

        return all(status != "fatal" for _, status, _ in self.validation) and not self.errors

    @property
    def accounting(self) -> str:
        """The one line that says whether the numbers add up (ADR-010)."""

        unaccounted = self.scanned - self.songs - self.skip_count
        parts = [
            f"{self.scanned} files scanned",
            f"{self.songs} songs catalogued",
            f"{self.skip_count} skipped",
        ]
        if unaccounted:
            parts.append(f"{unaccounted} unaccounted for")
        return " · ".join(parts)

    def counts(self) -> dict[str, int]:
        """The SAPRS 6.12 list, as data. Keys are that section's wording."""

        return {
            "files_discovered": self.discovered,
            "files_processed": self.processed,
            "files_skipped": self.skip_count,
            "metadata_repaired": self.repaired,
            "artwork_generated": self.artwork,
            "warnings": self.warning_count,
            "errors": self.error_count,
            "duplicates": len(self.duplicates),
            "song_count": self.songs,
            "validated": int(self.validated),
        }

    def text(self) -> str:
        """The report a human reads. Ordered as SAPRS 6.12 lists it."""

        lines: list[str] = [
            "Encore library build",
            "====================",
            f"  Scanned      {self.scanned:>7}  files under the music root",
            f"  Discovered   {self.discovered:>7}  supported audio files",
            f"  Processed    {self.processed:>7}  read and catalogued",
            f"  Skipped      {self.skip_count:>7}  not catalogued, listed below",
            f"  Repaired     {self.repaired:>7}  fields filled from MusicBrainz, path or filename",
            f"  Artwork      {self.artwork:>7}  images written to the cache",
            f"  Songs        {self.songs:>7}  rows in library.db",
            f"  Artists      {self.artists:>7}   ·   Albums {self.albums:>7}",
            f"  Duplicates   {len(self.duplicates):>7}",
            f"  Warnings     {self.warning_count:>7}   ·   Errors {self.error_count:>7}",
            "",
            f"  {self.accounting}",
            f"  MusicBrainz: {self.musicbrainz}",
            f"  Build: {'incremental' if self.incremental else 'full'}, {self.duration_seconds:.1f}s",
            "",
            "Validation",
        ]
        for name, status, detail in self.validation:
            lines.append(f"  [{status:<7}] {name:<16} {detail}")
        if self.skipped.total:
            lines += ["", "Skipped files"]
            lines += [f"  {line}" for line in self.skipped.lines()]
        if self.duplicates:
            lines += ["", "Possible duplicates"]
            lines += [f"  {line}" for line in self.duplicates[:DUPLICATE_LINES]]
            if len(self.duplicates) > DUPLICATE_LINES:
                lines.append(f"  … and {len(self.duplicates) - DUPLICATE_LINES} more")
        if self.warnings:
            lines += ["", "Warnings"]
            lines += [f"  {line}" for line in self.warnings]
        if self.errors:
            lines += ["", "Errors"]
            lines += [f"  {line}" for line in self.errors]
        if self.published_to is not None:
            lines += ["", f"Published {self.published_to} (library {self.library_version})"]
        else:
            lines += ["", "Not published."]
        return "\n".join(lines)

    def json(self) -> str:
        """The machine-readable report: `counts()` plus the detail text cannot express."""

        payload: dict[str, Any] = {
            **self.counts(),
            "files_scanned": self.scanned,
            "artists": self.artists,
            "albums": self.albums,
            "validation": [
                {"check": name, "status": status, "detail": detail}
                for name, status, detail in self.validation
            ],
            "skipped": dict(sorted(self.skipped.aggregate.items())),
            "skip_details": [
                {"path": str(entry.path), "reason": entry.reason, "detail": entry.detail}
                for entry in self.skipped.entries
            ],
            "duplicates": list(self.duplicates),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "duration_seconds": round(self.duration_seconds, 3),
            "library_version": self.library_version,
            "published": None if self.published_to is None else str(self.published_to),
            "incremental": self.incremental,
            "reused": self.reused,
            "musicbrainz": self.musicbrainz,
        }
        return json.dumps(payload, indent=2, sort_keys=True)


def describe(reason: str) -> str:
    """What a skip reason means, in the report's words rather than a code's."""

    return _REASONS.get(reason, reason)


def skipped_by(entries: Sequence[SkippedFile]) -> Counter[str]:
    """Counts per reason. Used by tests to assert the aggregate without the text."""

    return Counter(entry.reason for entry in entries)
