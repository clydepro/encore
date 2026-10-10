"""The views the shell renders: one read of the appliance, assembled once (SAPRS 9.4-9.9).

Now Playing and Up Next are the same question asked three ways — a fragment, a JSON
response, and the `snapshot` frame a reconnecting browser receives. The way that goes
wrong is each of them asking separately and getting three different answers during one
song change, which is why `player()` exists and reads once.

Three decisions live here rather than in a template or a router, because they are the
appliance's and not one screen's:

* **Up Next is capped.** `UP_NEXT_VISIBLE` is how many waiting rows a screen is given.
  `queue.max_items` bounds what a party can build; this bounds what a phone renders and a
  socket carries. The count beyond the cap travels with the list, so the screen can say
  "+7 more" instead of looking complete — the loose end `ai/HANDOFF.md` left for the
  fragment that exists now.
* **"Playing" and "queued" are said by the queue, not guessed by the caller.** SAPRS 9.7
  wants the difference shown, and `QueueService.enqueue()` already knows whether it
  started the head item. Inferring it from a later `SongStarted` would be guessing from an
  event that may belong to somebody else's request — ADR-011's argument, on the UI side.
* **Every label comes from a batch.** The rows are built by `encore/api/rows.py`, which
  resolves artists and albums two queries per page rather than two per row, because
  SAPRS 1.8's 100 ms search budget cannot survive a serialiser that reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from encore.api.deps import Jukebox
from encore.api.rows import (
    SongRow,
    rows_for_entries,
    rows_for_results,
    rows_for_songs,
)
from encore.domain import AlbumId, ArtistId, PlaybackState, SongId
from encore.search import SearchResults
from encore.services.library_service import MAX_PAGE_LIMIT

__all__ = [
    "BROWSE_LIMIT",
    "MAX_PAGE_LIMIT",
    "MAX_QUERY_LENGTH",
    "UP_NEXT_VISIBLE",
    "PlayerView",
    "album_context",
    "artist_context",
    "player",
    "queue_page",
    "results_context",
    "row_for_item",
    "song_row",
]

#: Waiting rows a screen is given. Twenty is about three scrolls on a phone.
UP_NEXT_VISIBLE = 20

#: Artists on a browse page (SAPRS 9.4). 200 is a tenth of a large library's artist
#: count and still one list, which is what makes a search box the right tool instead of a
#: pager.
BROWSE_LIMIT = 200

#: The widest `q=` Encore will parse, in either interface. A guard against a megabyte of
#: text reaching an FTS5 expression — the whole of SAPRS 10.9's "reasonable protection"
#: for search in v1, alongside the queue's own ceiling.
MAX_QUERY_LENGTH = 200

#: A row is "the current track" in these states and in none of the others. SAPRS 7.3's
#: `Loading` counts: the panel must name the track while mpv opens the file, and a guest
#: watching a blank Now Playing for a quarter of a second has learned nothing. `Paused`
#: counts too — the song has not ended, and a panel that renamed it "nothing" would be
#: describing the amplifier rather than the queue.
_CURRENT = frozenset({PlaybackState.LOADING, PlaybackState.PLAYING, PlaybackState.PAUSED})


@dataclass(frozen=True, slots=True, kw_only=True)
class PlayerView:
    """What is playing, what is waiting, and how long each has been."""

    state: PlaybackState
    now: SongRow | None
    up_next: list[SongRow]
    length: int
    max_items: int
    wait: timedelta
    position: timedelta
    duration: timedelta
    song_id: int | None

    @property
    def audible(self) -> bool:
        """Whether sound should be coming out of the box right now (SAPRS 7.3)."""

        return self.state is PlaybackState.PLAYING

    @property
    def current(self) -> bool:
        """Whether a row is the one the engine is on, sound or no sound."""

        return self.state in _CURRENT

    @property
    def queue_state(self) -> str:
        """The word SAPRS 9.7 asks a screen to show after a tap: playing, queued, idle."""

        if self.now is not None and self.current:
            return "playing"
        if self.length:
            return "queued"
        return "idle"

    @property
    def more_waiting(self) -> int:
        """Rows past the cap. Counted rather than hidden (SAPRS 9.9)."""

        return max(0, self.length - len(self.up_next) - (1 if self.now is not None else 0))

    @property
    def fraction(self) -> float:
        if not self.duration:
            return 0.0
        return min(1.0, max(0.0, self.position.total_seconds() / self.duration.total_seconds()))

    @property
    def context(self) -> dict[str, Any]:
        """The template context the panels render from. One place, one shape."""

        return {
            "view": self,
            "state": self.state,
            "audible": self.audible,
            "current": self.current,
            "now": self.now,
            "up_next": self.up_next,
            "length": self.length,
            "max_items": self.max_items,
            "wait": self.wait,
            "position": self.position,
            "duration": self.duration,
            "fraction": self.fraction,
            "more_waiting": self.more_waiting,
            "queue_state": self.queue_state,
        }

    def as_json(self) -> dict[str, Any]:
        """The `snapshot` frame's payload: enough for a browser to decide what to fetch,
        and nothing that can go stale. Whole rows are the API's business (SAPRS 10.2)."""

        return {
            "state": self.state.value,
            "song_id": self.song_id,
            "position_ms": round(self.position.total_seconds() * 1000),
            "duration_ms": round(self.duration.total_seconds() * 1000),
            "queue_length": self.length,
            "visible": len(self.up_next),
            "up_next": [
                {
                    "item_id": row.item_id,
                    "song_id": None if row.song is None else int(row.song.id),
                    "position": row.position,
                }
                for row in self.up_next
            ],
        }


def player(jukebox: Jukebox) -> PlayerView:
    """Read the appliance once.

    Runs on the appliance thread (ADR-012) and touches every service it reports on, which
    is why it is one function: a caller that wanted the queue and the engine separately
    would be two reads of a state that can change between them.
    """

    progress = jukebox.playback.progress
    current = jukebox.queue.now_playing()
    waiting = jukebox.queue.up_next(limit=UP_NEXT_VISIBLE)
    entries = ([current] if current is not None else []) + list(waiting)
    rows = rows_for_entries(entries, jukebox.library)
    now_row, up_next = (rows[0], rows[1:]) if current is not None else (None, rows)
    duration = progress.duration
    if not duration and now_row is not None and now_row.song is not None:
        # An engine that has not reported a length yet still has a track, and the library
        # knows how long it is. SAPRS 9.8 asks for "progress where appropriate": the
        # declared duration is the appropriate stand-in for the first second of a song.
        duration = now_row.song.duration
    return PlayerView(
        state=progress.state,
        now=now_row,
        up_next=up_next,
        length=jukebox.queue.length,
        max_items=jukebox.queue.max_items,
        wait=jukebox.queue.wait_time(),
        position=progress.position,
        duration=duration,
        song_id=None if progress.song_id is None else int(progress.song_id),
    )


def queue_page(jukebox: Jukebox) -> dict[str, Any]:
    """The whole waiting list, uncapped, for `/queue`.

    `UP_NEXT_VISIBLE` exists because a panel that ships forty rows to a phone on a hotspot
    is slow; a page whose subject *is* the list should not be an excuse to hide it. The two
    are the same rows built by the same two queries, so the only difference is how far down
    the screen you scroll (SAPRS 9.9).
    """

    current = jukebox.queue.now_playing()
    waiting = jukebox.queue.up_next(limit=None)
    entries = ([current] if current is not None else []) + list(waiting)
    return {
        "rows": rows_for_entries(entries, jukebox.library),
        "length": jukebox.queue.length,
        "max_items": jukebox.queue.max_items,
    }


def results_context(jukebox: Jukebox, text: str, *, limit: int | None = None) -> dict[str, Any]:
    """A search, as three labelled lists (SAPRS 9.6)."""

    results: SearchResults = jukebox.search.search(text, limit=limit)
    return {
        "query": text,
        "results": results,
        "songs": rows_for_results(results),
        "albums": list(results.albums),
        "artists": list(results.artists),
    }


def artist_context(jukebox: Jukebox, artist_id: ArtistId) -> dict[str, Any]:
    """One artist's screen: albums and tracks together, which is SAPRS 9.5's request.

    "The user should not need to repeatedly submit forms" is a sentence about a read
    pattern, and this is it: three queries for a whole artist, one of which is the artist.
    """

    artist = jukebox.library.artist(artist_id)
    if artist is None:
        return {"artist": None, "albums": [], "tracks": []}
    return {
        "artist": artist,
        "albums": jukebox.library.albums_for_artist(artist_id),
        "tracks": rows_for_songs(jukebox.library.songs_for_artist(artist_id), jukebox.library),
    }


def album_context(jukebox: Jukebox, album_id: AlbumId) -> dict[str, Any]:
    """One album, its artist, and its tracks in library order."""

    album = jukebox.library.album(album_id)
    if album is None:
        return {"album": None, "artist": None, "tracks": []}
    return {
        "album": album,
        "artist": jukebox.library.artist(album.artist_id),
        "tracks": rows_for_songs(jukebox.library.songs_for_album(album_id), jukebox.library),
    }


def song_row(jukebox: Jukebox, song_id: SongId) -> SongRow | None:
    """One labelled track, or None when the library has no such song."""

    song = jukebox.library.song(song_id)
    if song is None:
        return None
    return SongRow(
        song=song,
        artist=jukebox.library.artist(song.artist_id),
        album=jukebox.library.album(song.album_id),
    )


def row_for_item(view: PlayerView, item_id: int) -> SongRow | None:
    """The row in a view that a queue item became.

    Used by the answer to a guest's own request, so the screen can name the track it
    accepted without a second read of the library (SAPRS 9.7's "immediate feedback").
    """

    for row in [view.now, *view.up_next] if view.now else view.up_next:
        if row.item_id == item_id:
            return row
    return None
