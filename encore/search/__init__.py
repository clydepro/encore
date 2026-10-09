"""Search over artists, albums and songs using SQLite FTS5.

Target: sub-100 ms on a 15,000-song library (SAPRS 1.8, AIG 11). Search reads
through the library repositories and never owns SQL: the statements live in
`encore/repositories/library/queries.py` and ADR-009's contract test is what keeps
this package from becoming a second, drifting copy of them.

Three modules, one per decision:

* `query` — how guest text becomes an expression FTS5 can be given, and cannot reject.
* `results` — what a hit looks like when it has to identify itself on a phone screen
  (SAPRS 9.6).
* `service` — `SearchService`, the capability milestone 11's controllers call.

Nothing here imports HTTP, SQL drivers or mpv. Publication of search statistics
belongs to `StatisticsService`, and this package publishes no events: a search is a
read, and ADR-004's vocabulary is a list of facts, not of requests.
"""

from __future__ import annotations

from encore.search.errors import SearchError, SearchUnavailableError
from encore.search.query import MAX_TOKENS, MIN_PREFIX_LENGTH, SearchQuery, parse
from encore.search.results import (
    AlbumResult,
    ArtistResult,
    MatchedField,
    SearchResults,
    SongResult,
)
from encore.search.service import DEFAULT_PAGE_SIZE, CatalogueRead, SearchService

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_TOKENS",
    "MIN_PREFIX_LENGTH",
    "AlbumResult",
    "ArtistResult",
    "CatalogueRead",
    "MatchedField",
    "SearchError",
    "SearchQuery",
    "SearchResults",
    "SearchService",
    "SearchUnavailableError",
    "SongResult",
    "parse",
]
