"""Unit tests for `apps/installer/application.py`, the one definition of "the code" (#55).

These are pure-filesystem tests — no commands, no recording — because the module is a rule
about a tree rather than an actor on a machine. The three consumers (`execute.py`, `system.py`,
`plan.py`) are tested where they are used; what belongs here is the rule itself, including the
part that is easy to get subtly wrong: what counts as *not* the code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.installer.application import (
    EXCLUDED,
    difference,
    files,
    ignore,
    is_excluded,
    manifest,
    rsync_excludes,
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A checkout with one file of each kind that matters: code, artifact, database."""

    root = tmp_path / "checkout"
    (root / "encore" / "playback").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'encore'\n", encoding="utf-8")
    (root / "encore" / "__init__.py").write_text('__version__ = "0.5.0"\n', encoding="utf-8")
    (root / "encore" / "playback" / "mpv.py").write_text("class Mpv: ...\n", encoding="utf-8")
    (root / "apps" / "server").mkdir(parents=True)
    (root / "apps" / "server" / "main.py").write_text("def main(): ...\n", encoding="utf-8")
    return root


def written(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- the exclusion list --------------------------------------------------


def test_every_exclusion_is_a_name_or_a_glob_and_needs_to_be_in_both_dialects() -> None:
    """One list, so one place to be wrong.

    The 20-line duplicate rsync list #55 removed had a trailing slash on 11 of its entries and
    not on the other 9, which is the shape of a drift bug waiting to be written: an entry added
    to one list and not the other changes what a copy installs depending on whether the machine
    happens to have `rsync` on it.
    """

    assert "/" not in "".join(EXCLUDED), "rsync needs no directory markers"
    assert ".venv" in EXCLUDED
    assert "library.db" in EXCLUDED


def test_an_excluded_name_is_excluded_wherever_it_sits(tree: Path) -> None:
    """Pruning is by component, not by prefix: `build` is excluded at any depth.

    This is the rule `shutil.ignore_patterns` applies, and the rule `rsync` applies to a
    pattern with no leading slash, so both copies agree — which is the whole argument for
    dropping the trailing slashes the old rsync list carried.
    """

    for relative in (
        ".git/config",
        ".venv/bin/uv",
        "encore/__pycache__/config.cpython-312.pyc",
        "docs/build/index.html",
        "site/search/search_index.json",
        "var/library.db",
        "encore.egg-info/PKG-INFO",
        "library.db-wal",
        "opt/encore/venv/.encore-lock",
    ):
        written(tree, relative, "junk")
        assert any(is_excluded(part) for part in Path(relative).parts), relative
        assert relative not in manifest(tree), relative


def test_the_pruned_names_are_build_artifacts_and_not_paths_this_project_uses(
    tree: Path,
) -> None:
    """The one sharp edge of a component rule: it bites at every depth.

    `build`, `dist` and `site` are pruned everywhere, not only at the top where the docs and
    the wheel land. That is safe today because this repository's browser bundle is checked in
    under `encore/static/` — and it is worth saying out loud, because the day a build step
    starts writing `encore/static/dist/`, an install would copy a page with no CSS in it and
    every test of the installer would still pass. Add such a directory and this line is the
    one that fails first.
    """

    for relative in ("encore/static/css/encore.css", "encore/static/js/encore-live.js"):
        written(tree, relative, "/* bundled */")
        assert relative in manifest(tree), relative
    assert not any(
        is_excluded(component) for component in Path("encore/static/css/encore.css").parts
    )


def test_a_file_that_is_the_code_is_never_excluded(tree: Path) -> None:
    """The other half of the rule, and the half a too-eager pattern breaks: `pyproject.toml`,
    the templates, the static bundle and the migrations are all installed."""

    for relative in (
        "pyproject.toml",
        "uv.lock",
        "encore/__init__.py",
        "encore/templates/queue.html",
        "encore/static/css/encore.css",
        "encore/utilities/sqlite_api.py",
        "apps/server/main.py",
        "apps/builder/pipeline.py",
        "apps/installer/layout.py",
        "scripts/encore-party-sim",
        "README.md",
        "docs/SAPRS/Encore-SAPRS.md",
    ):
        written(tree, relative, "content")
        assert relative in manifest(tree), relative


def test_the_two_dialects_are_derived_not_duplicated() -> None:
    """`rsync_excludes()` and `ignore()` come from the same tuple, by construction.

    `ignore()` returns what to *drop* — `shutil.copytree`'s filter is the inverse of the
    inclusion list a reader might expect from the name `files`.
    """

    assert rsync_excludes() == tuple(f"--exclude={pattern}" for pattern in EXCLUDED)
    assert ignore()("somewhere", ["a.py", "library.db", "__pycache__"]) == {
        "library.db",
        "__pycache__",
    }


# -- manifests -----------------------------------------------------------


def test_a_manifest_names_files_by_relative_posix_path(tree: Path) -> None:
    assert set(manifest(tree)) == {
        "pyproject.toml",
        "encore/__init__.py",
        "encore/playback/mpv.py",
        "apps/server/main.py",
    }
    # Separator-normalised, so a comparison never fails over spelling alone.
    assert {relative for relative, _ in files(tree)} == set(manifest(tree))


def test_an_unreadable_file_is_not_a_match(tree: Path) -> None:
    """A file the installer cannot read reads as unknown, and unknown must not equal unknown.

    Two empty strings would compare equal, and an unreadable `encore/search/service.py` on one
    side would then be the same code as a readable one on the other.
    """

    relative = "encore/search/service.py"
    left = written(tree, relative, "class SearchService: ...\n")
    installed = manifest(tree)

    left.chmod(0o000)
    try:
        offered = manifest(tree)
    finally:
        left.chmod(0o644)
    if offered[relative] == installed[relative]:  # pragma: no cover — root reads anything
        pytest.skip("running as a user that chmod 000 cannot stop from reading")
    drift = difference(offered, installed)
    assert drift.changed == 1
    assert not drift.same


def test_a_missing_file_is_counted_as_missing_rather_than_different(tree: Path) -> None:
    """The #55 shape: new code in a release whose manifest did not change.

    `fault.py` arrived with Phase 5 — a file in one tree and not the other, which a comparison
    of two manifest texts has no way to see even when it is told to compare the right file.
    """

    offered = manifest(tree)
    written(tree, "encore/playback/fault.py", "class Supervisor: ...\n")
    installed = {relative: value for relative, value in offered.items() if "mpv" not in relative}
    drift = difference(manifest(tree), installed)
    assert drift.missing == 2
    assert not drift.changed
    assert not drift.extra
    assert drift.summary().startswith("2 missing")


def test_a_stale_file_is_counted_as_left_over() -> None:
    """What `rsync --delete` exists to prune, named rather than silently kept."""

    offered = {"encore/utilities.py": "1:aaa"}
    installed = {"encore/utilities.py": "1:aaa", "encore/old_playback.py": "9:bbb"}
    drift = difference(offered, installed)
    assert drift.extra == 1
    assert drift.summary().startswith("1 left over")


def test_identical_files_with_different_bytes_differ(tree: Path) -> None:
    """The bug #55 reported, in one assertion: a `git pull` that touches no manifest."""

    installed = manifest(tree)
    written(tree, "encore/playback/mpv.py", "class Mpv: ...\n# a Phase 6 bug fix\n")
    assert difference(manifest(tree), installed).summary() == "1 differ of 4 files"


def test_an_empty_tree_is_not_the_same_as_a_matching_one(tmp_path: Path) -> None:
    """`compared` is what separates "identical" from "there was nothing to read".

    An observer that reported a match for an unreadable source would plan no copy and print
    success, which is #55 again with the signs swapped.
    """

    drift = difference(manifest(tmp_path / "nowhere"), manifest(tmp_path / "gone"))
    assert drift.same
    assert not drift.identical
    assert not drift.compared
    assert drift.summary() == "nothing installed to compare"
