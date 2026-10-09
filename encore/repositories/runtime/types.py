"""Timestamps that cannot come back naive (SAPRS 5.3, ADR-009).

`ai/HANDOFF.md` makes this a phase rule: every stored timestamp is ISO 8601 with an
offset, because a naive value cannot be subtracted from an aware one. SQLite's
`TIMESTAMP` affinity does not care what it is given, and `CURRENT_TIMESTAMP` in a
`DEFAULT` yields a naive UTC string. The first time a queue item's `enqueued_at` is
subtracted from `utc_now()` across that line, the exception happens in a request
handler at a party, several layers away from the column that caused it.

So the conversion lives in the type, and the type is the only way to read or write
these columns. A `server_default=func.now()` still produces a naive value on the way
in — which is fine, because `process_result_value` runs on the way out, and one of the
two has to be authoritative.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.dialects import sqlite
from sqlalchemy.types import TypeDecorator

__all__ = ["UTCDateTime"]


class UTCDateTime(TypeDecorator[datetime]):
    """A `DateTime` that stores aware and returns aware.

    Attributes on the class rather than in a closure so the dialect and the compile
    step stay visible to anyone reading the emitted SQL.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        return dialect.type_descriptor(sqlite.TIMESTAMP())

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_literal_param(self, value: datetime | None, dialect: Any) -> str:
        """A literal rendering, for `literal_binds` compilation.

        SQLite has no timestamp literal type, so the value goes back as its ISO text —
        and None becomes `NULL`, because a literal that rendered as the string "None"
        would be stored and read as a name.
        """

        return "NULL" if value is None else repr(self.process_bind_param(value, dialect))

    @property
    def python_type(self) -> type[datetime]:
        return datetime
