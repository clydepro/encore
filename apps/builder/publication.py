"""Stage 10 — publication (SAPRS 5.8, 6.11; ADR-006, ADR-010).

`library.db` becomes the active library by an atomic rename, and nothing else. The
two requirements that sound the same and are not: 5.8 says a partially constructed
library must never be the active one, and 6.11 says a failed build must leave the
previous library untouched. A rename satisfies both only if it is a rename on the
filesystem that holds the destination.

That caveat is the reason this module is longer than `os.replace`. ADR-010 puts the
build in `paths.temp_dir`, and the reference layout (SAPRS 13.3) puts that on
`/var/cache` while the library lives on `/var/lib`. Those may be the same
filesystem and often are not, and `rename(2)` across them fails with `EXDEV` after
the work is done. So the sequence is: build in `temp_dir`, copy to a staging file
beside the destination, fsync the staging file, then rename within the filesystem.
A copy that fails leaves the staging file behind, never the destination.

`fsync` is not ceremony. On the flash card the appliance actually runs from (SAPRS
13.1), a rename that reaches the directory entry before the data does produces a
file that is valid until the power goes out, which is the one failure mode this
project cannot recover from at a party.
"""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

__all__ = ["PublicationError", "Published", "publish", "stage"]

#: Suffix of the staging file, beside the destination. Dotted and specific so an
#: operator's own backup of `library.db` never matches it.
STAGING_SUFFIX: str = ".staging"


class PublicationError(RuntimeError):
    """The new library could not be made the active one, or the old one untouched."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Published:
    """Where the artifact ended up.

    Attributes:
        path: The published `library.db`.
        replaced: True when a previous library was overwritten — the case ADR-009's
            reload path (SAPRS 6.11) has to handle, because a live Server may still
            hold the old file open by inode.
        staged_via: `rename` when the copy and the rename both happened, `direct`
            when the build file was already on the destination's filesystem.
    """

    path: Path
    replaced: bool = False
    staged_via: str = "rename"


def stage(connection: sqlite3.Connection, database: Path) -> Path:
    """Finish the SQLite file: commit, checkpoint, close, fsync.

    Returns the path, now durable and unopen-for-write. Every step is one the Server
    cannot do for us later: a `-wal` file left behind by a connection that was
    closed badly is exactly the artifact that reads as valid and is not.
    """

    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("PRAGMA optimize")
    connection.close()
    descriptor = os.open(database, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return database


def publish(built: Path, destination: Path) -> Published:
    """Make `built` the library at `destination`, atomically.

    Args:
        built: The staged file, usually under `paths.temp_dir`.
        destination: `paths.library_db`.

    Returns:
        Where it went and whether it replaced something.

    Raises:
        PublicationError: The copy, sync or rename failed. The destination is
            unchanged in every case — that is the whole point of the function, so a
            failure here must not be retried by a caller that assumes otherwise
            (SAPRS 6.11).
    """

    source = Path(built)
    target = Path(destination)
    if not source.is_file():
        raise PublicationError(f"nothing built at {source}; refusing to publish an empty library")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        # Including the case the operator causes: `paths.library_db` pointed at a
        # location whose parent is a file. The promise below is that a failed
        # publication says so as a `PublicationError`, and a `NotADirectoryError`
        # escaping to the CLI would print a traceback instead of that sentence.
        raise PublicationError(f"cannot prepare {target.parent}: {error}") from error
    staging = target.parent / f"{target.name}{STAGING_SUFFIX}"
    replaced = target.exists()
    via = "rename"

    try:
        if _same_filesystem(source, staging):
            stage_file = source
            via = "direct"
        else:
            _copy(source, staging)
            stage_file = staging
        _sync(staging if via == "rename" else source)
        os.replace(  # noqa: PTH105 - Path.replace is the same syscall with a shorter name
            staging if via == "rename" else source,
            target,
        )
        _sync_directory(target.parent)
        del stage_file
    except OSError as error:
        staging.unlink(missing_ok=True)
        raise PublicationError(
            f"cannot publish {source} to {target}: {error}. The previous library is unchanged."
        ) from error

    _cleanup(source)
    return Published(path=target, replaced=replaced, staged_via=via)


def _same_filesystem(left: Path, right: Path) -> bool:
    """Whether a rename between these two is atomic rather than impossible.

    Compared by device rather than by path string, because `/var/cache` may be a
    bind mount of a directory under `/var/lib` and a string comparison would then
    take the slow route for no reason.
    """

    def device(path: Path) -> int | None:
        probe = path
        while not probe.exists():
            probe = probe.parent
            if probe == probe.parent:
                return None
        return probe.stat().st_dev

    left_device, right_device = device(left), device(right)
    return left_device is not None and left_device == right_device


def _copy(source: Path, staging: Path) -> None:
    """Copy to the destination's filesystem, then fsync the copy before it can be named."""

    with source.open("rb") as read, staging.open("wb") as write:
        shutil.copyfileobj(read, write)
        write.flush()
        os.fsync(write.fileno())


def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    """Sync the directory entry, which is the half that survives an unplug.

    A file whose bytes are on the card and whose name is not is a file that does not
    exist, and the rename is precisely the operation that leaves that state if the
    directory is not flushed.
    """

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        if error.errno != errno.EACCES:
            raise
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Not every filesystem will fsync a directory (some return EINVAL); the file
        # itself is synced, so this costs durability of the name and nothing more.
        pass
    finally:
        os.close(descriptor)


def _cleanup(source: Path) -> None:
    """Remove the build file and any journal remnants. Failure is noise, not a fault."""

    for candidate in (
        source,
        Path(f"{source}-wal"),
        Path(f"{source}-shm"),
        Path(f"{source}-journal"),
    ):
        try:
            candidate.unlink()
        except OSError:
            continue
