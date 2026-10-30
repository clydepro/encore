"""The application tree: which files are the code, and how to tell two trees apart (#55).

One module owns that list because three places ask the question, and #55 is what happens when
they do not ask the same one:

* `execute.py` **copies** the tree (`rsync --delete`, or `shutil.copytree` where there is no
  rsync), and the exclusions decide what lands in `/opt/encore/application`.
* `system.py` **observes** it, to answer "does the installed code match what is being offered".
* `plan.py` **acts** on that answer.

Until now the copier had two hand-maintained copies of the rule — one in the dialect
`shutil.ignore_patterns` speaks and one in the dialect `rsync` speaks — and the observer had
no rule at all, because it was not looking at the tree. It was looking at
`pyproject.toml`. Sixteen source files differed on a two-releases-behind appliance, the
manifest matched byte for byte, and the report printed `nothing to do`
([#55](https://github.com/clydepro/encore/issues/55)).

The exclusions are written once, as names and glob patterns, and both dialects are derived
from it. The trailing slashes the rsync list used to carry are gone, and nothing changes:
`--exclude=build` excludes a file *or* a directory called `build`, and there is no reason for
this repository to contain either kind. What would be a bug is a name in one list and not the
other, and there is now one list to be wrong in.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "EXCLUDED",
    "Diff",
    "difference",
    "files",
    "identity",
    "ignore",
    "is_excluded",
    "manifest",
    "rsync_excludes",
]

#: Every path component a copy of this repository leaves out. Each entry is a build artifact,
#: a cache, or somebody's database — and the last category is the one that matters: carrying a
#: development `library.db` into `/opt/encore/application` would put a second library on a
#: machine that is allowed to have exactly one, in the wrong place, owned by root (ADR-006).
EXCLUDED: Final[tuple[str, ...]] = (
    ".git",
    ".venv",
    "__pycache__",
    "*.py[cod]",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".coverage",
    "coverage.xml",
    "htmlcov",
    "site",
    "dist",
    "build",
    "*.egg-info",
    ".encore-lock",
    "library.db",
    "runtime.db",
    "artwork",
    "*.db-wal",
    "*.db-shm",
)

#: Bytes read per SHA-256 update: one pass over a file, whatever its size.
_CHUNK: Final = 1 << 20

#: How many disagreeing paths a plan names. Enough to point at a release, few enough that a
#: line about 300 drifted files stays one line.
_LISTED: Final = 4


def is_excluded(name: str) -> bool:
    """Whether one path component — a file or a directory — is not part of the code.

    `fnmatch` is the matcher `shutil.ignore_patterns` uses, so this is the same predicate the
    copytree path applies rather than a re-implementation of it.
    """

    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDED)


def ignore() -> Callable[[str, list[str]], Iterable[str]]:
    """The `copytree(ignore=...)` filter, derived from `EXCLUDED`.

    Typed as `shutil.copytree` documents it rather than as the object `ignore_patterns`
    returns, whose class is private to the stdlib's own stubs.
    """

    return shutil.ignore_patterns(*EXCLUDED)


def rsync_excludes() -> tuple[str, ...]:
    """The same rule as `--exclude=` arguments."""

    return tuple(f"--exclude={pattern}" for pattern in EXCLUDED)


def files(root: Path) -> Iterator[tuple[str, Path]]:
    """Every file under `root` that a copy would carry, as `(relative posix path, path)`.

    `os.scandir` rather than `Path.rglob` because the directories have to be pruned, not
    filtered after the fact: `.venv` is 300 MB of site-packages on the machine an upgrade is
    planning itself on, and walking it to discard it is a slow way to ask a question the
    exclusion list already answers.
    """

    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            # An unreadable directory is not a difference. A destination mid-`chmod`, or a
            # mount that went away, reads as "nothing here", and the plan that follows from
            # that is a copy — which is the repair, not the failure.
            continue
        for entry in entries:
            if is_excluded(entry.name):
                continue
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                yield path.relative_to(root).as_posix(), path


def manifest(root: Path) -> dict[str, str]:
    """Content identities of everything a copy of `root` would carry.

    Digests, not `mtime` and not inode numbers: a `git pull` rewrites files and preserves
    nothing, an rsync can preserve timestamps, and the claim being checked is about bytes.
    """

    return {relative: identity(path) for relative, path in files(root)}


def identity(path: Path) -> str:
    """`size:sha256` of one file, in a string that compares for equality.

    The size is prefixed for the human reading a diff of two of these: `20:ab12…` against
    `21:ab12…` says "one byte added", which is a different conversation from "rewritten".
    Equality is still decided by the digest.
    """

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
        return f"{path.stat().st_size}:{digest.hexdigest()}"
    except OSError:
        # Unreadable is not "empty" — it is "unknown", and two unknowns must not compare
        # equal, or a file the installer cannot read on one side reads as identical.
        return f"unreadable:{path}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Diff:
    """How two trees disagree, in the counts a report can name.

    Attributes:
        changed: In both trees, different bytes.
        missing: In the offered tree, not in the installed one — the #55 case, and the one
            no manifest comparison would ever have seen: `encore/playback/fault.py` is new
            code in a release whose `pyproject.toml` did not change.
        extra: Installed and no longer offered — what `rsync --delete` exists to prune.
        compared: Paths seen on either side, so that `same` can be told apart from "nothing was
            read" — the two have the same diff and opposite plans.
        examples: A few disagreeing paths, for a dry run that has to say which.
    """

    changed: int = 0
    missing: int = 0
    extra: int = 0
    compared: int = 0
    examples: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.changed + self.missing + self.extra

    @property
    def same(self) -> bool:
        """Nothing differs. Also true of two trees that were never read, which is why
        `identical` exists and why `compared` is on the end of every sentence a report prints.
        """

        return self.total == 0

    @property
    def identical(self) -> bool:
        """Both sides were read, and they are the same code (#55's actual question)."""

        return self.same and self.compared > 0

    def summary(self) -> str:
        """One line, no adjectives: what an operator reads to decide whether to act."""

        if self.same:
            return "identical" if self.compared else "nothing installed to compare"
        parts = [
            f"{count} {word}"
            for word, count in (
                ("differ", self.changed),
                ("missing", self.missing),
                ("left over", self.extra),
            )
            if count
        ]
        return f"{', '.join(parts)} of {self.compared} files"

    def described(self) -> str:
        """`summary()` with the paths attached.

        #55 asked for the offenders in a dry run, and a count cannot be read aloud to a
        colleague over the phone: "it's `playback/fault.py`" can.
        """

        if self.same or not self.examples:
            return self.summary()
        rest = self.total - len(self.examples)
        return f"{self.summary()}: {', '.join(self.examples)}" + (
            f" (+{rest} more)" if rest > 0 else ""
        )


def difference(offered: Mapping[str, str], installed: Mapping[str, str]) -> Diff:
    """Compare two `manifest()` results, file by file."""

    shared = offered.keys() & installed.keys()
    changed = {relative for relative in shared if offered[relative] != installed[relative]}
    gone = _only_in(offered, installed)
    left = _only_in(installed, offered)
    return Diff(
        changed=len(changed),
        missing=len(gone),
        extra=len(left),
        compared=len(offered.keys() | installed.keys()),
        examples=tuple(sorted(changed | gone | left)[:_LISTED]),
    )


def _only_in(left: Mapping[str, str], right: Mapping[str, str]) -> set[str]:
    """Paths in one tree that the other does not have."""

    return left.keys() - right.keys()
