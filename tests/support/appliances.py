"""A composed appliance for a test, in one call (SAPRS 14.4's "no mpv, no network").

`apps/server/appliance.py:build` is the composition root, and a test that called it directly
would repeat the same four paths and two overrides in every file that needs a server. Two
things are worth having in one place rather than six:

* **The engine is always a double.** `FakeEngine` launches mock mpvs, so no test in the suite
  starts a process, opens a sound device, or fails on a machine with neither (SAPRS 14.4).
* **The queue ceiling is deliberately not the default.** 8 rather than 200 makes a configured
  limit distinguishable from a constant: an assertion that passes against the default would
  also pass against a hardcoded 200, which is the failure SAPRS 12.1 is about.

The tests that need a different number pass `max_items`; the ones that need to look at what
reached mpv keep the launcher the fixture handed them.
"""

from __future__ import annotations

from pathlib import Path

from apps.server.appliance import Appliance, build
from encore.config.models import EncoreConfig, PathsConfig, QueueConfig
from tests.support.mpv import FakeEngine


def composed_appliance(
    *,
    library_db: Path,
    runtime_db: Path,
    artwork_dir: Path,
    music_dir: Path,
    temp_dir: Path | None = None,
    max_items: int = 8,
    launcher: FakeEngine | None = None,
) -> Appliance:
    """An appliance over two real databases, with mock playback, not yet started."""

    engine = launcher or FakeEngine()
    return build(
        EncoreConfig(
            paths=PathsConfig(
                library_db=library_db,
                runtime_db=runtime_db,
                artwork_dir=artwork_dir,
                music_dir=music_dir,
                temp_dir=temp_dir or artwork_dir.parent / "temp",
            ),
            queue=QueueConfig(max_items=max_items),
        ),
        launcher=engine,
    )


def title_ids(library_db: Path) -> dict[str, int]:
    """Title → id, as the Builder numbered them — which is not the order they were written.

    The scan is by artist directory (ADR-010), so "First Album" is not album 1. A test that
    names a song and hardcodes an integer is a second copy of a fact one module owns, and it
    breaks on a rebuild rather than on a bug. Albums are included as `"album:<title>"`.

    Read through the real store rather than a fixture's memory: the point of the lookup is the
    agreement between what the Builder wrote and what the runtime reads back (SAPRS 14.2).
    """

    from encore.domain import AlbumId
    from encore.repositories.library import open_library

    store = open_library(library_db)
    try:
        found: dict[str, int] = {}
        for album in store.albums.all():
            found[f"album:{album.title}"] = int(album.id)
            for song in store.songs.by_album(AlbumId(int(album.id))):
                found[song.title] = int(song.id)
        return found
    finally:
        store.close()
