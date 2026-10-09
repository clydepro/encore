"""Numbered, forward-only `runtime.db` migrations (SAPRS 12.1, 12.2, ADR-009).

SAPRS 12.1 states the rule twice: *the installer must refuse to downgrade*. Refusing
means comparing the highest version recorded in the database against the highest
version this code knows, and raising when the database is ahead — which is what a
re-imaged SD card or a rolled-back package looks like from the appliance's side. There
is no `downgrade()` here, and that absence is the implementation of the rule: an
exercised-in-production rollback path nobody has tested is how a visible failure
becomes a corrupted queue.

The DDL is SQL as text rather than `Base.metadata.create_all()`, for the reason SAPRS
5.7 gives about the whole file: a build must produce the same artifact twice, and a
schema derived from whatever the models happened to say at import time is not that.
`models.py` therefore *describes* the same schema, and
`test_models_match_migrations` fails if the two disagree — which is the cost ADR-009
accepts when it lets an ORM define the runtime tables, paid for in one test.

`schema_version` is created as bootstrap, outside the migration list: a migration
table that records its own creation cannot be read before it exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from encore.repositories.contract import QUEUE_STATUSES
from encore.repositories.contract import RuntimeTable as RT
from encore.repositories.errors import RuntimeContractError

__all__ = ["CURRENT_VERSION", "MIGRATIONS", "Migration", "UpgradeReport", "upgrade"]

#: The schema revision this build understands. `runtime.schema_version` must not.
CURRENT_VERSION: Final = 1

#: Created once, before any migration runs, so there is somewhere to record that a
#: migration ran.
BOOTSTRAP: Final = (
    f"CREATE TABLE IF NOT EXISTS {RT.SCHEMA_VERSION} ("
    " version INTEGER PRIMARY KEY,"
    " name TEXT NOT NULL,"
    " applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
)

#: SQLite cannot `ALTER TABLE ... ADD CONSTRAINT`, so a constraint change is a rebuild:
#: create the new table, copy, drop, rename. Revision 2 of a migration will do that
#: here rather than pretend the limitation is a feature.
_INITIAL_TABLES: Final = f"""
CREATE TABLE {RT.QUEUE_ITEMS} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id     INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ({", ".join(f"'{item}'" for item in sorted(QUEUE_STATUSES))})),
    enqueued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    played_at   TIMESTAMP
);

CREATE INDEX ix_{RT.QUEUE_ITEMS}_position ON {RT.QUEUE_ITEMS} (position);
CREATE INDEX ix_{RT.QUEUE_ITEMS}_status ON {RT.QUEUE_ITEMS} (status);

CREATE TABLE {RT.PLAYBACK_HISTORY} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id     INTEGER NOT NULL,
    started_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    completion  REAL NOT NULL DEFAULT 0.0,
    outcome     TEXT
);

CREATE INDEX ix_{RT.PLAYBACK_HISTORY}_started ON {RT.PLAYBACK_HISTORY} (started_at);

CREATE TABLE {RT.RUNTIME_STATISTICS} (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    key     TEXT NOT NULL,
    value   INTEGER NOT NULL DEFAULT 0,
    day     TEXT NOT NULL DEFAULT '',
    UNIQUE (key, day)
);

CREATE TABLE {RT.ADMIN_STATE} (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash   TEXT NOT NULL,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at   TIMESTAMP
);
"""


@dataclass(frozen=True, slots=True)
class Migration:
    """One step forward.

    Attributes:
        version: Sequential from 1. A gap is a mistake rather than a pause: `upgrade`
            reads the recorded rows, so a missing number would mean "not yet run"
            forever.
        name: Recorded in `schema_version` and printed in the log, so a database can be
            read by a person.
        statements: Executed in order, inside one transaction.
    """

    version: int
    name: str
    statements: tuple[str, ...]


def _split(script: str) -> tuple[str, ...]:
    """One statement per `;`-separated chunk, blanks dropped.

    Splitting on semicolons is safe here and only here: this DDL contains no string
    literal holding one, and a migration that ever needs one needs a real parser, which
    is what this function is the note for.
    """

    return tuple(part.strip() for part in script.split(";") if part.strip())


MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(1, "initial runtime schema", _split(_INITIAL_TABLES)),
)


@dataclass(frozen=True, slots=True)
class UpgradeReport:
    """What an open did to the schema, so the log can say it and a test can assert it."""

    before: int
    after: int
    applied: tuple[int, ...]

    @property
    def created(self) -> bool:
        """Whether this run built the database from nothing."""

        return self.before == 0

    def describe(self) -> str:
        return f"runtime.db at schema version {self.after}" + (
            "" if not self.applied else f" (applied {', '.join(map(str, self.applied))})"
        )


def upgrade(engine: Engine) -> UpgradeReport:
    """Bring `engine`'s database to `CURRENT_VERSION`, or refuse.

    Statements run on a connection rather than through a `Session`: nothing here maps
    to an object, and an ORM session holding a transaction open across DDL is how a
    migration ends up nested inside itself. Each migration commits on its own, because
    SQLite's transactional DDL is real but not universal — `VACUUM` and some pragmas
    cannot run inside a transaction — so a failure leaves the database at the last
    version that completed, which is the only state a retry can reason about.

    Raises:
        RuntimeContractError: The recorded schema is ahead of this build (SAPRS 12.1).
    """

    with engine.begin() as connection:
        connection.execute(text(BOOTSTRAP))
        recorded = frozenset(
            int(row[0])
            for row in connection.execute(text(f"SELECT version FROM {RT.SCHEMA_VERSION}"))  # noqa: S608 - a contract constant
        )

    if ahead := sorted(version for version in recorded if version > CURRENT_VERSION):
        raise RuntimeContractError(
            f"runtime.db is at schema version {max(ahead)} and this Encore release "
            f"understands {CURRENT_VERSION}; refusing to downgrade (SAPRS 12.1). "
            "Restore the matching release, or start from a fresh runtime.db."
        )

    applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version in recorded:
            continue
        with engine.begin() as connection:
            for statement in migration.statements:
                connection.execute(text(statement))
            connection.execute(
                text(f"INSERT INTO {RT.SCHEMA_VERSION} (version, name) VALUES (:version, :name)"),  # noqa: S608
                {"version": migration.version, "name": migration.name},
            )
        applied.append(migration.version)
    return UpgradeReport(
        before=min(recorded, default=0),
        after=max(recorded, default=CURRENT_VERSION),
        applied=tuple(applied),
    )


def recorded_version(engine: Engine) -> int:
    """The highest version in the database, or 0 when there is no schema table yet.

    Reads the table by name rather than through the models, because `upgrade` needs the
    answer before the models are known to match what is on disk.
    """

    try:
        with engine.connect() as connection:
            value = connection.execute(
                text(f"SELECT MAX(version) FROM {RT.SCHEMA_VERSION}")  # noqa: S608 - a constant name
            ).scalar()
    except SQLAlchemyError:
        return 0
    return int(value or 0)
