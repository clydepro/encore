"""Shared fixtures and test-suite wiring (PBK Chapter 16).

Two things happen here:

1. Test categories are marked automatically from their directory, so authors
   write `def test_...` in `tests/regression/` and get the `regression` marker
   without remembering to decorate it.
2. The infrastructure required by PBK 16 is exposed as fixtures: disposable
   SQLite databases, a mock mpv, a media directory and an FTS5 capability gate.

Long-running suites (performance, Party Simulation, anything `slow`) are
excluded from the default run and opt in with `--run-slow`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.builder.pipeline import BuildOptions, run
from apps.builder.report import BuildReport
from encore.repositories.library import LibraryStore, open_library
from encore.repositories.runtime import RuntimeStore, open_runtime_store
from tests.support.media import AlbumSpec, cover_image, generate_albums
from tests.support.mpv import MockMpv
from tests.support.sqlite import TempDatabase, fts5_available, temp_database

TESTS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True, slots=True)
class BuiltLibrary:
    """A published library, the options that built it, and its report.

    A fixture returning a three-field value rather than a path because almost every
    test that wants the database also wants to assert on what the Builder said it did.
    """

    report: BuildReport
    options: BuildOptions


CATEGORIES: dict[str, str] = {
    "unit": "unit",
    "integration": "integration",
    "regression": "regression",
    "performance": "performance",
    "party_simulation": "party_simulation",
}

#: Markers that mean "not in the per-commit gate" (SAPRS 14.15).
MARKED_SLOW: frozenset[str] = frozenset({"slow", "performance", "party_simulation"})


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add Encore-specific command line options."""

    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="Include slow, performance and party-simulation tests.",
    )
    parser.addoption(
        "--party-profile",
        action="store",
        default=str(TESTS_DIR / "party_simulation" / "profiles" / "house_party.toml"),
        help="Load profile used by the Party Simulation suite (SAPRS 14.12).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply category markers by directory and gate the long-running suites."""

    if not config.getoption("--run-slow"):
        skip_slow = pytest.mark.skip(reason="enable with --run-slow (scheduled in CI)")
        for item in items:
            if MARKED_SLOW & {marker.name for marker in item.iter_markers()}:
                item.add_marker(skip_slow)

    for item in items:
        category = _category_for(Path(str(item.fspath)))
        if category is not None:
            item.add_marker(getattr(pytest.mark, category))


def _category_for(path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(TESTS_DIR)
    except ValueError:
        return None
    parts = relative.parts[:-1]
    for part in parts:
        if part in CATEGORIES:
            return CATEGORIES[part]
    return None


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def encore_home(tmp_path: Path) -> Path:
    """A sandbox mirroring the appliance layout from SAPRS 13.3."""

    home = tmp_path / "encore"
    for directory in ("application", "var/lib", "var/cache", "etc"):
        (home / directory).mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def library_db(encore_home: Path) -> Iterator[TempDatabase]:
    """An empty, immutable-at-runtime `library.db` in the sandbox."""

    with temp_database(encore_home / "var/lib", "library.db") as database:
        yield database


@pytest.fixture
def runtime_db(encore_home: Path) -> Iterator[TempDatabase]:
    """A mutable `runtime.db` in the sandbox."""

    with temp_database(encore_home / "var/lib", "runtime.db") as database:
        yield database


@pytest.fixture
def mpv() -> MockMpv:
    """A mock mpv instance: no audio hardware, no subprocess (SAPRS 14.4)."""

    return MockMpv()


@pytest.fixture
def media_dir(encore_home: Path) -> Path:
    """Empty directory for synthetic media (see `tests/support/media.py`)."""

    directory = encore_home / "music"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def runtime_store(encore_home: Path) -> Iterator[RuntimeStore]:
    """A migrated, WAL-verified `runtime.db` store, closed after the test."""

    store = open_runtime_store(encore_home / "var/lib" / "runtime.db")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def music_tree(tmp_path: Path) -> Path:
    """A small generated corpus in the `<artist>/<album>/` layout ADR-010 reads.

    Its own directory rather than `media_dir`, because `media_dir` is empty by design
    and several tests need a scan that finds something.
    """

    root = tmp_path / "music"
    generate_albums(
        [
            AlbumSpec.with_tracks(
                "The Test Artists", "First Album", count=3, artwork=cover_image()
            ),
            AlbumSpec.with_tracks("The Test Artists", "Second Album", count=2, fmt="flac"),
            AlbumSpec.with_tracks("Someone Else", "Third Album", count=2),
        ],
        root,
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def built_library(encore_home: Path, music_tree: Path) -> BuiltLibrary:
    """One real Builder run over `music_tree`, published into the sandbox.

    The fixture is the pipeline rather than a hand-written schema because the thing
    these tests check is the agreement between the two applications (ADR-010), and an
    agreement asserted against a copy of one side proves nothing.
    """

    options = BuildOptions(
        music_dir=music_tree,
        library_db=encore_home / "var/lib" / "library.db",
        artwork_dir=encore_home / "var/cache" / "artwork",
        temp_dir=encore_home / "var/cache" / "builder",
        library_version="test",
    )
    return BuiltLibrary(report=run(options), options=options)


@pytest.fixture
def library_store(built_library: BuiltLibrary) -> Iterator[LibraryStore]:
    """The published `library.db`, opened through the read-only store."""

    store = open_library(built_library.options.library_db)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def sqlite_fts5() -> None:
    """Skip the test when this interpreter's SQLite lacks FTS5 (SAPRS 5.5)."""

    connection = sqlite3.connect(":memory:")
    try:
        if not fts5_available(connection):
            pytest.skip("SQLite build without FTS5; search tests skipped")
    finally:
        connection.close()
