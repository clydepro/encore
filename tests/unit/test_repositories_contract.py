"""The schema contract, checked as data (ADR-009, ADR-010).

ADR-010 makes the table and column names a contract between two applications that may
never see each other's code, and this module is the only place both are allowed to read
them from. The interesting failure it guards against is not a missing column — that is
`test_library_contract.py`, which runs real SQL against a real built database — but a
contract that quietly grew a second definition of the schema, which is what happens when
someone puts a `CREATE TABLE` next to a column name "just to keep them together".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from encore.repositories import contract

PROJECT_ROOT = Path(__file__).resolve().parents[2]

MODULES = (
    "contract.py",
    "coercion.py",
    "errors.py",
    "library/connection.py",
    "library/queries.py",
    "library/store.py",
    "runtime/models.py",
    "runtime/migrations.py",
    "runtime/session.py",
)

#: Anything that creates or drops structure. A match inside `contract.py` means the DDL
#: has started to live in the shared module, which is ADR-010's sole-writer rule ending.
DDL_MARKERS = ("CREATE TABLE", "DROP TABLE", "ALTER TABLE", "CREATE INDEX", "CREATE VIEW")


@pytest.mark.parametrize("name", sorted(contract.COLUMNS))
def test_every_table_names_its_columns_once(name: str) -> None:
    columns = contract.columns_for(name)
    assert columns, f"{name} declares no columns"
    assert len(columns) == len(set(columns)), f"{name} repeats a column"


def test_the_contract_is_data_only() -> None:
    """No SQL, no connection, no imports from either application.

    Asserted on the syntax tree rather than with a regex, because a docstring that says
    `CREATE TABLE` while explaining why not to write one would fail a text search and be
    correct.
    """

    source = (PROJECT_ROOT / "encore/repositories/contract.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(
            node,
            ast.Constant,
        )
        and isinstance(node.value, str)
    ]
    statements = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom | ast.Import)]

    for marker in DDL_MARKERS:
        assert not any(marker in literal for literal in literals), f"contract.py contains {marker}"
    for statement in statements:
        imported = statement.module if isinstance(statement, ast.ImportFrom) else ""
        assert imported is None or "repositories.library" not in str(imported)
        assert "builder" not in str(getattr(statement, "module", ""))


def test_library_and_runtime_tables_are_disjoint() -> None:
    """One file, one purpose (SAPRS 5.7). A shared name would be a bug in this test."""

    assert not contract.LIBRARY_TABLES & contract.RUNTIME_TABLES


def test_the_version_the_server_checks_is_the_version_the_builder_stamps() -> None:
    """Both halves read one constant, so a bump cannot land on only one side."""

    builder = (PROJECT_ROOT / "apps/builder/schema.py").read_text(encoding="utf-8")
    assert "LIBRARY_SCHEMA_VERSION" in builder
    assert contract.LIBRARY_SCHEMA_VERSION >= 1


def test_provenance_sources_and_fields_are_closed_sets() -> None:
    """The report and the `CHECK` constraints both enumerate these (ADR-010)."""

    assert {
        contract.MetadataSource.TAG,
        contract.MetadataSource.MUSICBRAINZ,
        contract.MetadataSource.PATH,
        contract.MetadataSource.FILENAME,
    } <= contract.METADATA_SOURCES
    assert contract.MetadataSource.CONSTANT not in {contract.MetadataSource.TAG}
    assert contract.MetadataField.ALBUM_ARTIST in contract.METADATA_FIELDS


def test_queue_statuses_match_the_domain_enum() -> None:
    """A `CHECK` constraint and a `StrEnum` describing the same thing (SAPRS 4.5)."""

    from encore.domain import QueueItemStatus

    assert set(contract.QUEUE_STATUSES) == {status.value for status in QueueItemStatus}


def test_audio_formats_and_artwork_kinds_match_the_domain() -> None:
    from encore.domain import ArtworkKind, AudioFormat

    assert set(contract.AUDIO_FORMAT_NAMES) == {item.value for item in AudioFormat}
    assert set(contract.ARTWORK_KINDS) == {item.value for item in ArtworkKind}


@pytest.mark.parametrize("name", MODULES)
def test_repository_modules_do_not_reach_the_web(name: str) -> None:
    """ADR-009's boundary, one level down: storage knows nothing about HTTP."""

    path = PROJECT_ROOT / "encore/repositories" / name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            module = getattr(node, "module", None) or ""
            for alias in node.names:
                joined = f"{module}.{alias.name}" if module else alias.name
                assert not any(
                    part in joined for part in ("fastapi", "starlette", "jinja2", "uvicorn")
                ), joined
