"""Row → Python value coercion, written down once (ADR-009, SAPRS 5.3).

ADR-009 lists this as a cost it accepts rather than a problem it ignores: SQLite
has *affinity*, not type. `BOOLEAN` returns `int` through raw `sqlite3` and `bool`
through SQLAlchemy; a datetime stored as `TEXT` comes back as `TEXT` from either;
a column declared `INTEGER` will happily hand back a float if a `REAL` was stored
in it. Strict mypy makes that visible as `no-any-return` on every raw row read,
which is the point — it forces the conversion to exist somewhere, and "somewhere"
should be one reviewed module rather than twelve mappers each inventing their own.

`RowView` is that somewhere. It is deliberately tiny and deliberately not a
general data-access layer: it converts, and when a value is not the type the
schema promises, it raises `ContractViolationError` naming the column, because
that means the Builder and the Server disagree about a name or a type and a
reviewer needs to see which.

Nothing here decides what a value *means*. A row becomes a `Song` in
`encore/repositories/library/mappers.py`; rules belong to the domain.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Final

from encore.repositories.errors import ContractViolationError
from encore.utilities.clock import ensure_aware

__all__ = ["RowView", "iso_timestamp", "parse_timestamp"]

#: The separator `datetime.isoformat()` puts between the time and its offset.
_UTC_TEXT: Final = "+00:00"


def iso_timestamp(value: datetime) -> str:
    """Render an aware datetime as the text both databases store.

    ISO 8601 with an explicit offset, which is what `ai/HANDOFF.md` requires of
    every stored timestamp: a naive value in `runtime.db` cannot be subtracted
    from an aware one, and queue order, playback history and statistics all do
    exactly that. Normalising to UTC first keeps one instant written one way, so
    `ORDER BY enqueued_at` on text orders by time.
    """

    return ensure_aware(value, field_name="timestamp").astimezone(UTC).isoformat()


def parse_timestamp(text: str) -> datetime:
    """Read back what `iso_timestamp` wrote, refusing a naive value.

    Raises:
        ValueError: The text carries no offset. `ensure_aware` is the check, and
            it is on the read path precisely because SQLite will return whatever
            a stray writer stored.
    """

    parsed = datetime.fromisoformat(text)
    return ensure_aware(parsed, field_name="stored timestamp")


class RowView:
    """Typed reads from one row, with the table named for honest errors.

    Accepts a `sqlite3.Row` or any mapping, so the same coercion answers for the
    read side and for anything built out of SQLAlchemy rows.
    """

    __slots__ = ("_row", "_table")

    def __init__(self, row: sqlite3.Row | Mapping[str, Any], table: str) -> None:
        self._row = row
        self._table = table

    @property
    def raw(self) -> sqlite3.Row | Mapping[str, Any]:
        return self._row

    def keys(self) -> tuple[str, ...]:
        """Column names this row carries, for mapper tests."""

        return tuple(self._row.keys())

    def text(self, column: str) -> str:
        value = self._value(column)
        if not isinstance(value, str):
            raise self._wrong(column, value)
        return value

    def optional_text(self, column: str) -> str | None:
        value = self._value(column)
        if value is None or isinstance(value, str):
            return value
        raise self._wrong(column, value)

    def integer(self, column: str) -> int:
        value = self._value(column)
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._wrong(column, value)
        return value

    def optional_integer(self, column: str) -> int | None:
        value = self._value(column)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._wrong(column, value)
        return value

    def number(self, column: str) -> float:
        """A stored duration or ratio: `REAL` in the schema, `float` here.

        An `int` is accepted, because SQLite's numeric affinity will hand one
        back for a value of `3.0` written as `3`, and calling that a contract
        violation would make the check about a formatting accident.
        """

        value = self._value(column)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise self._wrong(column, value)
        return float(value)

    def optional_number(self, column: str) -> float | None:
        value = self._value(column)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise self._wrong(column, value)
        return float(value)

    def boolean(self, column: str) -> bool:
        """`int` in SQLite, `bool` in Python (ADR-009's measured example)."""

        value = self._value(column)
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        raise self._wrong(column, value)

    def duration(self, column: str) -> timedelta:
        """Seconds-as-real to `timedelta`, the unit `encore.domain` speaks."""

        return timedelta(seconds=self.number(column))

    def optional_duration(self, column: str) -> timedelta | None:
        value = self.optional_number(column)
        return None if value is None else timedelta(seconds=value)

    def timestamp(self, column: str) -> datetime:
        value = self.text(column)
        try:
            return parse_timestamp(value)
        except ValueError as error:
            raise self._wrong(column, value) from error

    def optional_timestamp(self, column: str) -> datetime | None:
        value = self.optional_text(column)
        return None if value is None else parse_timestamp(value)

    def path(self, column: str, *, absolute: bool = True) -> Path:
        value = Path(self.text(column))
        if absolute and not value.is_absolute():
            raise self._wrong(column, value)
        return value

    def enumeration[E: Enum](self, column: str, enum_type: type[E]) -> E:
        """Map a stored label onto an `encore.domain` enum.

        A value outside the enum is the schema and the domain disagreeing — a
        Builder built from another revision, or a `CHECK` constraint that did not
        hold — and is reported as such rather than as a bare `ValueError`.
        """

        value = self.text(column)
        try:
            return enum_type(value)
        except ValueError as error:
            raise self._wrong(column, value) from error

    def optional_enumeration[E: Enum](self, column: str, enum_type: type[E]) -> E | None:
        value = self.optional_text(column)
        if value is None:
            return None
        try:
            return enum_type(value)
        except ValueError as error:
            raise self._wrong(column, value) from error

    def _value(self, column: str) -> Any:
        """One named column, with a missing name reported as the contract break it is.

        `sqlite3.Row` raises `IndexError` and a mapping raises `KeyError` for the same
        mistake, and neither names the column. ADR-009's mitigation is that a rename
        should say so, which for a read path means the exception has to be raised here
        rather than converted by every caller.
        """

        try:
            return self._row[column]
        except (KeyError, IndexError) as error:
            raise ContractViolationError(self._table, column) from error

    def _wrong(self, column: str, value: object) -> ContractViolationError:
        return ContractViolationError(self._table, column, got=value)
