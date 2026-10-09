"""Stage 9 — validation, the publication gate (SAPRS 6.10, 6.11; ADR-010).

Eight checks, all of them against the built file rather than against the in-memory
tracks, because the thing under test is the artifact the Server will open. A
conclusion that was correct in `Track` and wrong in a row is exactly the bug this
stage exists to catch, and it is the kind that survives every other kind of test.

ADR-010 assigns `PRAGMA integrity_check` and `foreign_key_check` to this gate
rather than to Server startup, and the reason is worth keeping in mind before
moving one: the Server opens `library.db` on every boot and must do it in well under
a second on a Pi 4, so a check that costs a second belongs here, once, offline.

A finding is **fatal** when publishing would mean shipping a database that cannot
be played from, and a **warning** when the library works and the operator should
know. Missing rows, broken keys and a corrupt file are fatal. A file that has gone
missing from the share since discovery, or an album with no artwork, are not: the
first is a network drive being a network drive, and the second is 100% of the FLAC
in the measured corpus (SAPRS 6.7).
"""
# ruff: noqa: S608 — every interpolated name is a `contract` constant, and the checks
# are reads of the artifact this package just wrote (SAPRS 6.10).

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from encore.repositories.contract import (
    LIBRARY_SCHEMA_VERSION,
    LIBRARY_TABLES,
    METADATA_FIELDS,
    METADATA_SOURCES,
    SEARCH_VIEWS,
    Table,
)

__all__ = ["Finding", "Severity", "ValidationReport", "validate"]


class Severity:
    """Whether a finding stops a publication. Two values, deliberately."""

    FATAL = "fatal"
    WARNING = "warning"


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    """One check's outcome. `passed` findings are kept, not dropped.

    A validation report that lists only failures cannot answer "did you even check
    the foreign keys?", which is the first question an operator asks about a
    database that played the wrong file.
    """

    name: str
    passed: bool
    severity: str = Severity.WARNING
    detail: str = ""
    count: int = 0

    @property
    def label(self) -> str:
        return "ok" if self.passed else self.severity


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationReport:
    """Everything the gate decided about one built file."""

    findings: Sequence[Finding] = field(default=())

    @property
    def valid(self) -> bool:
        """True when nothing fatal was found. Warnings never make this False."""

        return not self.failures

    @property
    def failures(self) -> tuple[Finding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if not finding.passed and finding.severity == Severity.FATAL
        )

    @property
    def warnings(self) -> tuple[Finding, ...]:
        """Findings that say "look at this" without saying "do not ship this".

        Filtered on severity and not merely on `passed`, because a report that listed a
        fatal finding twice — once as a failure and once as a warning — would let a
        caller that reads only the warnings section publish a broken library.
        """

        return tuple(
            finding
            for finding in self.findings
            if not finding.passed and finding.severity == Severity.WARNING
        )

    def line(self, name: str) -> Finding | None:
        for finding in self.findings:
            if finding.name == name:
                return finding
        return None


def validate(
    connection: sqlite3.Connection,
    *,
    expected_songs: int,
    artwork_dir: Path | None = None,
    music_root: Path | None = None,
    schema_version: int = LIBRARY_SCHEMA_VERSION,
) -> ValidationReport:
    """Run every check in SAPRS 6.10 against a built, unpublished library.

    Args:
        connection: The build connection, committed or not — the checks are reads.
        expected_songs: How many tracks the pipeline concluded were catalogable.
            Compared against the rows, because a song lost between a `Track` and an
            `INSERT` is the failure mode this whole stage exists for.
        artwork_dir: Check that every `artwork.relative_path` names a real file.
            Skipped when None, which the `--no-artwork` path uses.
        music_root: Check that every stored path is under the music root. The check
            is containment rather than existence: an incremental build reads files
            it did not discover, and one moved directory should be a warning.
        schema_version: The contract this Builder writes. Checked against the stamp
            it just wrote, not against the DDL text, so a schema edit that forgets
            the constant fails here.

    Returns:
        A report. Never raises for a failed check; only for a database SQLite
        cannot read at all, which propagates as it should.
    """

    findings = [
        _schema(connection, schema_version=schema_version),
        _integrity(connection),
        _foreign_keys(connection),
        _required_fields(connection),
        _provenance(connection),
        _song_count(connection, expected=expected_songs),
        _paths(connection, music_root=music_root),
        _artwork(connection, artwork_dir=artwork_dir),
        _search_indexes(connection),
    ]
    return ValidationReport(findings=tuple(findings))


def _schema(connection: sqlite3.Connection, *, schema_version: int) -> Finding:
    """The artifact contains the tables, views and stamp the contract names."""

    present = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing = sorted((LIBRARY_TABLES | SEARCH_VIEWS) - present)
    stamp = _meta(connection, "schema_version")
    if missing:
        return Finding(
            name="schema",
            passed=False,
            severity=Severity.FATAL,
            detail=f"missing: {', '.join(missing)}",
            count=len(missing),
        )
    if stamp != str(schema_version):
        return Finding(
            name="schema",
            passed=False,
            severity=Severity.FATAL,
            detail=f"stamped {stamp!r}, this Builder writes {schema_version}",
        )
    return Finding(
        name="schema",
        passed=True,
        detail=f"{len(LIBRARY_TABLES)} tables, {len(SEARCH_VIEWS)} views",
    )


def _integrity(connection: sqlite3.Connection) -> Finding:
    """`PRAGMA integrity_check` — ADR-010 puts this here, not at Server startup."""

    rows = connection.execute("PRAGMA integrity_check").fetchall()
    answer = str(rows[0][0]) if rows else "no result"
    passed = answer == "ok"
    return Finding(
        name="integrity",
        passed=passed,
        severity=Severity.FATAL,
        detail=answer if not passed else "sqlite reports no corruption",
        count=0 if passed else len(rows),
    )


def _foreign_keys(connection: sqlite3.Connection) -> Finding:
    """`PRAGMA foreign_key_check` — SAPRS 5.3 makes foreign keys mandatory."""

    rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    return Finding(
        name="foreign keys",
        passed=not rows,
        severity=Severity.FATAL,
        detail="" if not rows else f"{len(rows)} rows point at nothing",
        count=len(rows),
    )


def _required_fields(connection: sqlite3.Connection) -> Finding:
    """Rows that were built have the fields a row must have (SAPRS 6.10).

    ADR-010 narrows 6.5's requirement to built rows on purpose: a file that could
    not be catalogued is not a validation failure, it is a skip, and calling it a
    failure here would abort the build for the 15 files the last build reported.
    """

    rows = connection.execute(
        f"SELECT COUNT(*) FROM {Table.SONGS} AS s"
        f" JOIN {Table.MUSIC_FILES} AS f ON f.id = s.music_file_id"
        f" JOIN {Table.ALBUMS} AS a ON a.id = s.album_id"
        f" JOIN {Table.ARTISTS} AS r ON r.id = s.artist_id"
        " WHERE length(trim(s.title)) = 0 OR length(trim(f.path)) = 0 OR f.duration_seconds <= 0"
        "    OR length(trim(a.title)) = 0 OR length(trim(r.name)) = 0"
    ).fetchone()
    count = int(rows[0])
    return Finding(
        name="required fields",
        passed=count == 0,
        severity=Severity.FATAL,
        detail="" if count == 0 else f"{count} song rows missing a mandatory value",
        count=count,
    )


def _provenance(connection: sqlite3.Connection) -> Finding:
    """Every built song has a record of where its values came from.

    ADR-010 makes the per-field record mandatory — "it is the only way to answer
    'why is this song under this artist?'" — so it is checked rather than trusted.
    """

    rows = connection.execute(
        f"SELECT COUNT(*) FROM {Table.SONGS} AS s WHERE NOT EXISTS ("
        f" SELECT 1 FROM {Table.METADATA} AS m WHERE m.song_id = s.id)"
    ).fetchone()
    missing = int(rows[0])
    sources = {
        str(row[0]) for row in connection.execute(f"SELECT DISTINCT source FROM {Table.METADATA}")
    }
    unknown = sorted(sources - METADATA_SOURCES)
    fields = {
        str(row[0]) for row in connection.execute(f"SELECT DISTINCT field FROM {Table.METADATA}")
    }
    bogus_fields = sorted(fields - METADATA_FIELDS)
    if unknown or bogus_fields:
        return Finding(
            name="provenance",
            passed=False,
            severity=Severity.FATAL,
            detail=f"values outside the contract: {', '.join(unknown + bogus_fields)}",
        )
    return Finding(
        name="provenance",
        passed=missing == 0,
        severity=Severity.WARNING,
        detail="" if missing == 0 else f"{missing} songs with no field provenance",
        count=missing,
    )


def _song_count(connection: sqlite3.Connection, *, expected: int) -> Finding:
    count = int(connection.execute(f"SELECT COUNT(*) FROM {Table.SONGS}").fetchone()[0])
    return Finding(
        name="song count",
        passed=count == expected,
        severity=Severity.FATAL,
        detail=f"{count} rows for {expected} catalogued tracks",
        count=count,
    )


def _paths(connection: sqlite3.Connection, *, music_root: Path | None) -> Finding:
    """Stored paths are absolute and inside the library root (SAPRS 6.10).

    Existence is not checked, and that is not an oversight. The appliance may build
    on one machine and play on another (ADR-001), and a build that failed because a
    share was not mounted at the same path would punish the operator for the
    deployment rather than describe the library.
    """

    rows = connection.execute(
        f"SELECT path FROM {Table.MUSIC_FILES} WHERE substr(path, 1, 1) != '/'"
    ).fetchall()
    detail = ""
    passed = not rows
    severity = Severity.FATAL
    if rows:
        detail = f"{len(rows)} paths are not absolute"
    elif music_root is not None:
        outside = _paths_outside(connection, music_root=music_root)
        if outside:
            passed = False
            # The two halves of this check are not equally serious, and one severity
            # for both would be wrong in each direction. A relative path can never be
            # opened, so it is fatal. A path outside the *given* root is a claim about
            # the deployment: the same library builds and plays on another machine
            # (ADR-001), and an incremental run legitimately carries files it did not
            # discover this time. That one warns.
            severity = Severity.WARNING
            detail = f"{outside} paths are outside {music_root}"
    return Finding(
        name="file paths",
        passed=passed,
        severity=severity,
        detail=detail or "all paths absolute",
        count=len(rows),
    )


def _paths_outside(connection: sqlite3.Connection, *, music_root: Path) -> int:
    prefix = str(music_root.absolute()).rstrip("/") + "/"
    rows = connection.execute(f"SELECT path FROM {Table.MUSIC_FILES}").fetchall()
    return sum(1 for row in rows if not str(row[0]).startswith(prefix))


def _artwork(connection: sqlite3.Connection, *, artwork_dir: Path | None) -> Finding:
    """Every reference resolves to a file that is actually in the cache.

    A warning rather than fatal, and SAPRS 6.7 is why: "missing artwork must not
    make a track unplayable". An artwork row whose file is absent means a guest sees
    a default tile, which is the outcome 6.7 already accepted.
    """

    rows = connection.execute(f"SELECT id, relative_path FROM {Table.ARTWORK}").fetchall()
    if artwork_dir is None:
        return Finding(
            name="artwork", passed=True, detail=f"{len(rows)} references, cache not checked"
        )
    missing = sum(1 for _, relative in rows if not (artwork_dir / str(relative)).is_file())
    return Finding(
        name="artwork",
        passed=missing == 0,
        severity=Severity.WARNING,
        detail="" if missing == 0 else f"{missing} of {len(rows)} images missing from the cache",
        count=missing,
    )


def _search_indexes(connection: sqlite3.Connection) -> Finding:
    """The derived structures hold one entry per canonical row (SAPRS 5.4, 6.10)."""

    counts: dict[str, tuple[int, int]] = {
        "song": (_rows(connection, Table.SONGS), _rows(connection, Table.SONG_SEARCH)),
        "album": (_rows(connection, Table.ALBUMS), _rows(connection, Table.ALBUM_SEARCH)),
        "artist": (_rows(connection, Table.ARTISTS), _rows(connection, Table.ARTIST_SEARCH)),
    }
    wrong = [
        f"{name}: {indexed} indexed for {canonical} rows"
        for name, (canonical, indexed) in counts.items()
        if indexed != canonical
    ]
    return Finding(
        name="search indexes",
        passed=not wrong,
        severity=Severity.FATAL,
        detail="; ".join(wrong),
        count=sum(indexed for _, indexed in counts.values()),
    )


def _rows(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _meta(connection: sqlite3.Connection, key: str) -> str:
    row = connection.execute(
        f"SELECT value FROM {Table.LIBRARY_META} WHERE key = ?", (key,)
    ).fetchone()
    return "" if row is None else str(row[0])


def summary(report: ValidationReport) -> Mapping[str, int]:
    """Counts for `BuildCompleted.validated` and the report header."""

    return {
        "checks": len(report.findings),
        "passed": sum(1 for finding in report.findings if finding.passed),
        "warnings": sum(
            1
            for finding in report.findings
            if not finding.passed and finding.severity == Severity.WARNING
        ),
        "failures": len(report.failures),
    }
