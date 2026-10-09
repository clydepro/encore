"""Stage 1 — discovery (SAPRS 6.2, 6.3, ADR-010).

One rule decides whether a file is in the library: `AudioFormat.for_path` returns
a format or it does not. That is the entire filter, and it is the domain's rule
rather than this module's, so "what Encore can play" has one answer shared with
playback, the tests and the corpus measurements in ADR-010.

Three decisions in here are ADR-010's rather than mine:

* **Unsupported files are counted, not itemised.** The measured corpus holds 188
  `.m4p` (DRM), 41 `.wma` and 2 `.aif` — 231 real tracks that will never play.
  Listing them individually produces a 231-line warning block whose only effect is
  to train the operator to skip past the report, so discovery returns one count
  per suffix and keeps the paths for the detail file.
* **Nothing is modified, moved or deleted** (SAPRS 6.8). This stage does not even
  open a file: it `stat()`s one. An unreadable file is a skip with a reason, not
  an error, so one bad directory cannot stop the other 335 being indexed.
* **`size_bytes` and `mtime_ns` are collected here**, because they are two of the
  four parts of the incremental key (SAPRS 6.9) and a second pass to fetch them
  would race with the files changing underneath it.

Hidden and AppleDouble entries are ignored rather than reported. `.DS_Store` is
not music and its absence from the report is not information; `._Track.mp3` would
otherwise be read, fail, and appear as a defect the operator cannot fix.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from apps.builder.records import DiscoveredFile
from encore.domain.media import AudioFormat

__all__ = ["IGNORED_NAMES", "NON_AUDIO_SUFFIXES", "Discovery", "discover"]

#: Names that are never music on any platform Encore installs on. `._x.mp3` is
#: macOS's resource-fork shadow of `x.mp3`; indexing it would report the same track
#: twice and fail to play either copy.
IGNORED_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".DS_Store",
        "desktop.ini",
        "Thumbs.db",
        # GarageBand saves a song as a `x.band` *directory*; its internals are an
        # application package rather than a recording. `projectData` and `PkgInfo`
        # carry no extension at all, so suffix matching cannot see them, and counting
        # five of those as "music Encore cannot play" would be the report describing
        # the filesystem instead of the decision.
        "projectdata",
        "pkginfo",
    }
)

#: Suffixes that are not audio in any container. The distinction from "unsupported"
#: is what keeps ADR-010's aggregate honest: `188 .m4p` is a story about a library
#: Encore cannot play, and `412 .jpg` is a story about the filesystem. Both belong in
#: the report and neither belongs beside the other.
#:
#: `.m3u8` is here despite being a container-shaped name because a playlist is a list
#: of files, not a file of samples, and `AudioFormat.for_path` has nothing to say about
#: it either way.
NON_AUDIO_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".tif",
        ".tiff",
        ".txt",
        ".pdf",
        ".html",
        ".htm",
        ".xml",
        ".json",
        ".yaml",
        ".yml",
        ".cue",
        ".log",
        ".m3u",
        ".m3u8",
        ".pls",
        ".wpl",
        ".asx",
        ".plist",
        ".url",
        ".lnk",
        ".lrc",
        ".lyrc",
        ".srt",
        ".ass",
        ".sub",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".so",
        ".py",
        ".pyc",
        ".sh",
        ".bat",
        # `.mp4` is deliberately absent: it is a legal AAC container, and
        # `AudioFormat.for_path` claims it (SAPRS 1.2 lists AAC and M4A without
        # distinguishing them). A file discovery accepted is not a file this list
        # gets to reject.
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        ".flv",
        ".wmv",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Discovery:
    """What a scan of one directory tree found.

    Attributes:
        files: Supported files, sorted by path so two runs over an unchanged tree
            produce comparable output and an incremental diff that means
            something.
        unsupported: Count per lowercase suffix, for the aggregate line SAPRS 6.3
            and ADR-010 ask for. Derived from `unsupported_files` rather than kept
            beside it, because the two disagreeing is the bug and there is no reason
            to allow the disagreement.
        unreadable: Entries that could not be read or `stat()`ed, with the OS
            reason. Reported, never dropped.
        scanned: Every file looked at, ignored ones included. ADR-010 wants this
            beside the final song count, because a skipped song and a song that
            was never there are indistinguishable in a database.
    """

    files: Sequence[DiscoveredFile] = ()
    unsupported_files: tuple[Path, ...] = ()
    unreadable: tuple[tuple[Path, str], ...] = ()
    scanned: int = 0

    @property
    def unsupported(self) -> Counter[str]:
        return Counter(_suffix(path) for path in self.unsupported_files)

    @property
    def supported_count(self) -> int:
        return len(self.files)

    @property
    def unsupported_count(self) -> int:
        return sum(self.unsupported.values())

    def aggregate(self) -> str:
        """The one line the report prints for everything it could not play."""

        if not self.unsupported:
            return "no unsupported files"
        listed = ", ".join(
            f"{count} {suffix}" for suffix, count in sorted(self.unsupported.items())
        )
        return f"{self.unsupported_count} files skipped: {listed}"


def discover(root: Path) -> Discovery:
    """Recursively find playable files under `root`.

    Args:
        root: `paths.music_dir` (ADR-010, default `/opt/music`). Read by the
            Builder only — the Server never scans the filesystem (SAPRS 12.2).

    Returns:
        A `Discovery`. A missing or unreadable root yields an empty one instead of
        raising: the pipeline turns "0 files found under X" into a warning with
        the path in it, which is a report an operator can act on rather than a
        traceback that names a line of Python.
    """

    found: list[DiscoveredFile] = []
    unsupported: list[Path] = []
    unreadable: list[tuple[Path, str]] = []
    scanned = 0
    for path, stat in _walk(root, unreadable):
        if _ignored(path):
            continue
        scanned += 1
        format_ = AudioFormat.for_path(path)
        if format_ is None:
            if _suffix(path) not in NON_AUDIO_SUFFIXES:
                unsupported.append(path)
            continue
        found.append(
            DiscoveredFile(
                path=path,
                format=format_,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    found.sort(key=lambda item: str(item.path))
    return Discovery(
        files=tuple(found),
        unsupported_files=tuple(sorted(unsupported, key=str)),
        unreadable=tuple(unreadable),
        scanned=scanned,
    )


def _walk(root: Path, problems: list[tuple[Path, str]]) -> Iterator[tuple[Path, os.stat_result]]:
    """Yield `(path, stat)` for every file under `root`, in directory order.

    The `stat` travels with the path rather than being fetched later for two
    reasons: it is the same syscall, and it is taken at the moment the entry was
    seen. A file that changes during a 20 GB scan is a rarer problem than a file
    that changes between scanning and indexing it, and this ordering makes the
    recorded size and timestamp at least agree with each other.

    A `PermissionError`, a deleted directory or an unreadable entry is recorded
    and the walk continues. The failure has to be visible: a library that quietly
    lost an artist because of one `chmod` is the silent failure `ai/HANDOFF.md`
    warns about one level up.
    """

    stack: list[Path] = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                ordered = sorted(entries, key=lambda entry: entry.name)
        except OSError as error:
            # The root is not recorded as an unreadable *entry*: the pipeline
            # checks `scanned == 0` and says the clearer thing about the root.
            if directory != root:
                problems.append((directory, str(error)))
            continue
        for entry in ordered:
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not entry.name.startswith("."):
                        stack.append(path)
                    continue
                yield path, entry.stat(follow_symlinks=False)
            except OSError as error:
                problems.append((path, str(error)))


def _ignored(path: Path) -> bool:
    """Whether this path is not a candidate at all, so `scanned` should not count it.

    A hidden *directory* is skipped by name and not by walking into it and filtering:
    `.git/objects` on a synced library tree is tens of thousands of entries, and
    discovery is the one stage that has to be cheap enough to run on a Pi.
    """

    if path.suffix.lower() in NON_AUDIO_SUFFIXES:
        # A cover image is not a file the library could contain, and counting it in
        # `scanned` would put it in the arithmetic an operator checks the report
        # against. The same reasoning that removes `.DS_Store` removes `folder.jpg`.
        return True
    name = path.name
    if name.lower() in IGNORED_NAMES or name.startswith(("._", ".")):
        return True
    return any(part.startswith(".") for part in path.parts[:-1])


def _suffix(path: Path) -> str:
    return path.suffix.lower() or "<no extension>"
