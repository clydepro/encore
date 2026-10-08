"""Library facts (SAPRS 5.8, 6.11, 6.12, AIG 8).

One rule binds both events here: they report a *published* library, never a
partially built one (SAPRS 5.8). A build that failed emits nothing, so no
subscriber can act on a database that is not the active one.
"""

from __future__ import annotations

from dataclasses import dataclass

from encore.events.base import Event

__all__ = ["BuildCompleted", "LibraryReloaded"]


@dataclass(frozen=True, slots=True, kw_only=True)
class LibraryReloaded(Event):
    """A running server switched to a newly published `library.db` (SAPRS 6.11).

    Attributes:
        song_count: Tracks now available - the number a guest is told when the
            box changes under them.
        version: Builder-supplied identifier for the published library. Kept as
            text because the Builder's manifest (milestone 6) owns its format.
    """

    song_count: int = 0
    version: str = ""

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        if self.song_count < 0:
            raise ValueError(f"song_count cannot be negative, got {self.song_count}")


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildCompleted(Event):
    """A Builder run finished and published (SAPRS 6.12, AIG 6).

    Carries the counts SAPRS 6.12 requires a build report to state. Published by
    the Builder, whose only connection to the runtime is this event type - the
    Builder must not import playback components (ADR-001), and importing
    `encore.events` is what lets it report a finished build without doing that.
    """

    files_discovered: int = 0
    files_processed: int = 0
    files_skipped: int = 0
    metadata_repaired: int = 0
    artwork_generated: int = 0
    warnings: int = 0
    errors: int = 0
    song_count: int = 0
    validated: bool = False

    def __post_init__(self) -> None:
        Event.__post_init__(self)
        for name, value in (
            ("files_discovered", self.files_discovered),
            ("files_processed", self.files_processed),
            ("files_skipped", self.files_skipped),
            ("song_count", self.song_count),
        ):
            if value < 0:
                raise ValueError(f"{name} cannot be negative, got {value}")
        if self.files_processed + self.files_skipped > self.files_discovered:
            raise ValueError(
                f"a build cannot process {self.files_processed} and skip {self.files_skipped} "
                f"of {self.files_discovered} discovered files"
            )
