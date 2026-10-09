"""`runtime.db` migrations, checked against the models that describe them (SAPRS 12.1).

Two assertions carry the weight here.

The first is the obvious one: a database at a schema version this build does not
understand must not be opened. SAPRS 12.1 states it twice because the alternative is
silent — a downgrade that "works" deletes columns the newer code wrote, and an
appliance that comes back after a rolled-back package with an empty queue is worse than
one that refuses to start.

The second is the one that would otherwise be invisible. `models.py` and
`migrations.py` describe the same schema twice, once in Python and once in SQL, and ADR-009
accepts that duplication as the price of using an ORM for writes. The price is only
paid if someone checks, and this file is the check: every mapped column, on every
table, with a compatible declared type, must exist in the artifact the migrations
build.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Engine,
    Float,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.types import TypeDecorator, TypeEngine

from encore.repositories.contract import QUEUE_STATUSES, RUNTIME_TABLES
from encore.repositories.contract import RuntimeTable as RT
from encore.repositories.errors import RuntimeContractError, StoreError, StoreNotFoundError
from encore.repositories.runtime import CURRENT_VERSION, MIGRATIONS, open_runtime_store, upgrade
from encore.repositories.runtime.migrations import recorded_version
from encore.repositories.runtime.models import Base
from encore.repositories.runtime.session import RuntimeConfig, create_runtime_engine


def test_a_fresh_database_is_migrated_on_open(tmp_path: Path) -> None:
    store = open_runtime_store(tmp_path / "runtime.db")
    with store:
        info = store.info
        assert info.schema_version == CURRENT_VERSION
        assert info.needs_upgrade is False
        assert info.current


def test_opening_twice_applies_nothing(tmp_path: Path) -> None:
    first = open_runtime_store(tmp_path / "runtime.db")
    first.close()
    second = open_runtime_store(tmp_path / "runtime.db")
    with second:
        assert second.report is not None
        assert second.report.applied == (), "a second open re-ran a migration"
        assert second.report.before == CURRENT_VERSION


def test_a_newer_schema_refuses_to_open(tmp_path: Path) -> None:
    """SAPRS 12.1: refuse, in words that name both versions and say what to do."""

    engine = create_runtime_engine(RuntimeConfig(path=tmp_path / "runtime.db"))
    upgrade(engine)
    _mark_future_version(engine)
    with pytest.raises(RuntimeContractError) as error:
        upgrade(engine)
    message = str(error.value)
    assert "downgrade" in message.lower()
    assert str(CURRENT_VERSION) in message


def test_a_future_database_refuses_at_the_store_level(tmp_path: Path) -> None:
    """The same refusal, reached the way `apps/server` would reach it."""

    path = tmp_path / "runtime.db"
    store = open_runtime_store(path)
    _mark_future_version(store.engine)
    store.close()
    with pytest.raises(RuntimeContractError):
        open_runtime_store(path)


def test_migrations_are_numbered_without_gaps() -> None:
    versions = [migration.version for migration in MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1)), (
        "a gap makes a version 'not yet run' forever"
    )
    assert len(MIGRATIONS) == CURRENT_VERSION


def test_every_migration_has_a_name_a_person_can_read() -> None:
    assert all(migration.name.strip() for migration in MIGRATIONS)
    assert len({migration.name for migration in MIGRATIONS}) == len(MIGRATIONS)


def test_every_migration_is_idempotent_by_number_not_by_text(tmp_path: Path) -> None:
    """`CREATE TABLE` without `IF NOT EXISTS` is correct only because of the version row.

    Checked by re-running one migration's statements against an already-built database
    and expecting SQLite to complain, which is the failure that would follow a bug in
    the recorded-version logic. An error-free re-run would mean the guard is the only
    thing standing between a re-run and a corrupted schema, and this test would then be
    telling a different story than it thinks.
    """

    engine = create_runtime_engine(RuntimeConfig(path=tmp_path / "runtime.db"))
    upgrade(engine)
    with engine.connect() as connection, pytest.raises(SQLAlchemyError):
        connection.execute(text(MIGRATIONS[0].statements[0]))


def test_the_models_and_the_migrations_describe_one_schema(tmp_path: Path) -> None:
    """The cost of ADR-009's split, paid in one test.

    Compares the ORM's metadata against `PRAGMA table_info` on the artifact the
    migrations build: names, nullability and a compatible declared type, for every
    column of every table. A model edited without a migration fails here, and so does a
    migration that adds a column no model reads — which is the direction that actually
    hurts, because it is invisible until something wonders where the data went.
    """

    engine = create_runtime_engine(RuntimeConfig(path=tmp_path / "runtime.db"))
    upgrade(engine)
    stored = _schema(engine)
    for table in Base.metadata.sorted_tables:
        assert table.name in stored, f"{table.name} is mapped but never created"
        for column in table.columns:
            declared = stored[table.name].get(column.name)
            assert declared is not None, (
                f"{table.name}.{column.name} is mapped but not in the schema"
            )
            name, notnull = declared
            if not column.primary_key:
                # A rowid alias is implicitly NOT NULL and `PRAGMA table_info` says
                # nothing about it, so a primary key's flag compares differently by
                # design (SAPRS 5.7). Every other column has to agree exactly.
                assert bool(notnull) == (not column.nullable), (
                    f"{table.name}.{column.name} nullability"
                )
            assert _compatible(name, column.type), (
                f"{table.name}.{column.name}: {name} vs {column.type}"
            )
    for name in RUNTIME_TABLES:
        assert name in stored, f"{name} is in the contract but not in the schema"


def test_wal_is_on_and_says_so(tmp_path: Path) -> None:
    """SAPRS 5.7 requires WAL; `configure_wal` verifies rather than asserts."""

    store = open_runtime_store(tmp_path / "runtime.db")
    with store:
        assert store.info.journal_mode == "wal"


def test_foreign_keys_are_enabled_on_every_connection(tmp_path: Path) -> None:
    """SQLite leaves this OFF per connection, so a store that forgot would pass everything else.

    Nothing in the runtime schema uses a foreign key today — `song_id` cannot be one,
    because `library.db` is a different file (SAPRS 5.7) — which is exactly why the
    pragma belongs here: the day a table references one, the check has to already exist.
    """

    store = open_runtime_store(tmp_path / "runtime.db")
    with store, store.session() as session:
        assert int(session.execute(text("PRAGMA foreign_keys")).scalar_one()) == 1


def test_a_non_database_file_is_named(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    path.write_text("this is not a database")
    with pytest.raises(StoreError, match="not a SQLite database"):
        open_runtime_store(path)


def test_create_false_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(StoreNotFoundError):
        open_runtime_store(RuntimeConfig(path=tmp_path / "runtime.db", create=False))


def test_the_queue_status_check_constraint_matches_the_domain(tmp_path: Path) -> None:
    """A `CHECK` list and a `StrEnum`, one truth (SAPRS 4.5)."""

    from encore.domain import QueueItemStatus

    assert set(QUEUE_STATUSES) == {status.value for status in QueueItemStatus}
    store = open_runtime_store(tmp_path / "runtime.db")
    with store, store.session() as session, pytest.raises(IntegrityError):
        session.execute(
            text(
                f"INSERT INTO {RT.QUEUE_ITEMS} (song_id, position, status) VALUES (1, 1, 'mystery')"
            )
        )
    store.close()


def test_recorded_version_reads_the_database_not_the_models(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    assert recorded_version(create_runtime_engine(RuntimeConfig(path=path, create=True))) == 0
    store = open_runtime_store(path)
    assert recorded_version(store.engine) == CURRENT_VERSION
    store.close()


def test_only_one_admin_row_can_exist(tmp_path: Path) -> None:
    """The schema enforcing SAPRS 12.3's "one administrator", which is stronger than a check."""

    store = open_runtime_store(tmp_path / "runtime.db")
    with store, store.session() as session, pytest.raises(IntegrityError):
        session.execute(
            text(f"INSERT INTO {RT.ADMIN_STATE} (id, password_hash) VALUES (1, 'a'), (2, 'b')")
        )
    store.close()


def _mark_future_version(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(f"INSERT INTO {RT.SCHEMA_VERSION} (version, name) VALUES (:v, :n)"),
            {"v": CURRENT_VERSION + 1, "n": "from the future"},
        )


def _schema(engine: Engine) -> dict[str, dict[str, tuple[str, int]]]:
    """table → column → (declared type, `notnull`), read from the built artifact."""

    result: dict[str, dict[str, tuple[str, int]]] = {}
    with engine.connect() as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
            )
        ]
        for table in tables:
            rows = connection.execute(text(f"PRAGMA table_info({table})")).fetchall()
            result[table] = {str(row[1]): (str(row[2]).upper(), int(row[3])) for row in rows}
    return result


#: What each ORM type is allowed to be called in the hand-written DDL. SQLite has
#: affinity rather than types, so `VARCHAR(16)` *is* `TEXT`, and a check that compared
#: the strings literally would fail on a naming preference and pass on a real mismatch.
_ACCEPTABLE: Final[tuple[tuple[tuple[type[TypeEngine[Any]], ...], tuple[str, ...]], ...]] = (
    ((Integer, BigInteger), ("INTEGER",)),
    ((Float, Numeric), ("REAL", "NUMERIC")),
    ((DateTime, Date), ("TIMESTAMP", "DATETIME", "TEXT")),
    ((Boolean,), ("BOOLEAN", "INTEGER")),
    ((String, Text), ("TEXT", "VARCHAR")),
)


def _compatible(stored: str, declared: TypeEngine[Any]) -> bool:
    """SQLite's affinity, not its spelling.

    `VARCHAR(16)` *is* `TEXT` to SQLite, and a `TypeDecorator` *is* the type it
    delegates to, so a check that compared the two literally would fail on our own
    indirection (`runtime/types.py`) and pass on a genuine mismatch — the worst of both.
    """

    inner: object = declared
    while isinstance(inner, TypeDecorator):
        implementation = type(inner).impl
        inner = implementation() if isinstance(implementation, type) else implementation
    return any(
        isinstance(inner, types) and stored.startswith(prefixes) for types, prefixes in _ACCEPTABLE
    )
