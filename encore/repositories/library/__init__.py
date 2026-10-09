"""`library.db` reads: raw `sqlite3`, `mode=ro`, nothing else (SAPRS 5.2, ADR-009).

Everything the running Server knows about the music comes through this package, and the
package is deliberately narrow: one connection class that cannot write, one module of
`SELECT` statements, one module of row mappers, and repositories per aggregate. An ORM
is not used here — SAPRS 3.3 and ADR-009 explain why — and nothing in it is importable
as a write path, so "the runtime never modifies `library.db`" is enforced by a file
mode rather than by a review comment.

The boundary rule (ADR-009: no `Engine`, `Session`, `Row` or `Result` crosses out) is
enforceable here for one reason: every method's return type is a name from
`encore.domain`. A `sqlite3.Row` cannot be returned by accident when the only door out
of a query takes a mapper.
"""

from __future__ import annotations

from encore.repositories.library.catalogue import AlbumRepository, ArtistRepository
from encore.repositories.library.connection import LibraryConnection
from encore.repositories.library.media import (
    ArtworkRepository,
    MusicFileRepository,
    ProvenanceRepository,
)
from encore.repositories.library.search import (
    AlbumHit,
    ArtistHit,
    LibrarySearch,
    SearchRead,
    SongHit,
)
from encore.repositories.library.songs import SongRepository
from encore.repositories.library.store import (
    REQUIRED_TABLES,
    LibraryInfo,
    LibraryStore,
    open_library,
)

__all__ = [
    "REQUIRED_TABLES",
    "AlbumHit",
    "AlbumRepository",
    "ArtistHit",
    "ArtistRepository",
    "ArtworkRepository",
    "LibraryConnection",
    "LibraryInfo",
    "LibrarySearch",
    "LibraryStore",
    "MusicFileRepository",
    "ProvenanceRepository",
    "SearchRead",
    "SongHit",
    "SongRepository",
    "open_library",
]
