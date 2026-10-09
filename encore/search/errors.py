"""Failure to search at all, as distinct from nothing matching (SAPRS 11.2, 5.5)."""

from __future__ import annotations

__all__ = ["SearchError", "SearchUnavailableError"]


class SearchError(RuntimeError):
    """Base for the search feature's failures.

    Its own hierarchy rather than the repository's, so that a controller in milestone 11
    can map failures without importing anything from `encore.repositories` — the
    dependency direction SAPRS 15.2 wants, which otherwise erodes one `except` at a time.

    The names deliberately mirror the repository's: `repositories.errors.SearchUnavailableError`
    is the storage condition and this is the feature's report of it, translated in
    `service.py`. Two spellings for one fact would be worse than one shared name.
    """


class SearchUnavailableError(SearchError):
    """The library has no search index, so no answer can be given.

    A Pi built against a SQLite without FTS5, or a library published before the search
    structures existed, is a supported way to lose this feature. Returning an empty
    result list would be a lie of the kind SAPRS 11.2 names: indistinguishable from a
    library in which nobody has ever heard of the song.
    """
