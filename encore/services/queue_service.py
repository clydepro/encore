"""The Queue Service: strict FIFO, anonymous guests, one rule per sentence (SAPRS Chapter 8).

What belongs here is the *rule*, not the storage — `encore/repositories/runtime/queue.py`
already keeps rows, positions and statuses, and rewriting that logic here would be the
second definition ADR-009 exists to prevent. What is added is the behaviour a guest
experiences: accepting a request, deciding whether it plays now or later, advancing when a
track ends, and keeping the queue's word stable across a refresh and a restart.

The rules, each traced to the line that requires it:

* **FIFO, always** (SAPRS 8.1, 8.5). Order is enqueue order and nothing else: no priority,
  no reordering, no "play this now" that is not an administrative removal followed by the
  normal head selection.
* **Duplicates allowed** (SAPRS 8.3). Nothing in this module compares one request's song
  to another's. That is the whole implementation of the prohibition on de-duplication, and
  `queue.allow_duplicates` cannot be switched off (SAPRS 12.2, `QueueConfig`).
* **Idle means begin** (SAPRS 8.2). A request that arrives while nothing is audible starts
  the *head* of the queue rather than itself, because the alternative is newest-first by
  accident — and a queue that reorders on arrival contradicts 8.5 the moment someone
  presses stop and someone else queues.
* **Advancement is the sequence in 8.7**, in that order: report, mark, select, begin,
  announce. Step 1 is the engine's publication, so this service starts at step 2.
* **The maximum is explicit** (SAPRS 8.4). `queue.max_items` is enforced and the refusal
  names the number.

**Why the service calls the player directly** (SAPRS 8.2's "begin playback" cannot be
expressed as a fact that already happened) and why the player never calls back: ADR-011.
Everything else travels on the bus — the queue subscribes to `SongFinished` and
`PlaybackRecovered` rather than being wired into the engine — so the dependency is one-way
and the recovery path needs no knowledge of who owns the queue.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol, runtime_checkable

from encore.config.models import QueueConfig
from encore.domain import (
    PlaybackOutcome,
    PlaybackState,
    QueueItem,
    QueueItemId,
    QueueItemStatus,
    Song,
    SongId,
)
from encore.events.bus import EventBus, Subscription
from encore.events.playback import FinishedReason, PlaybackRecovered, SongFinished
from encore.events.queue import QueueAdvanced, SongQueued
from encore.services.errors import QueueFullError, SongNotAvailableError
from encore.utilities.clock import Clock, SystemClock

__all__ = [
    "SETTLEMENT",
    "Player",
    "QueueEntry",
    "QueueService",
    "QueueStore",
    "SongLookup",
]

#: How a finished track leaves the queue. Four reasons, four answers, and each one is a
#: sentence in the specification rather than a preference: a completion is history (8.7),
#: a skip is history out of order (8.8), a failure was never playable (11.9), and a stop
#: put the song back at the head because nobody heard the end of it (8.5).
SETTLEMENT: Final[dict[FinishedReason, QueueItemStatus]] = {
    FinishedReason.COMPLETED: QueueItemStatus.FINISHED,
    FinishedReason.SKIPPED: QueueItemStatus.SKIPPED,
    FinishedReason.FAILED: QueueItemStatus.REMOVED,
    FinishedReason.STOPPED: QueueItemStatus.PENDING,
}

#: Bound on how far the advancement loop will walk forward over unusable items. A queue is
#: already capped by `queue.max_items`, so this exists only so that a bug in `_play_head`
#: costs a log line rather than the process.
_MAX_WALK = 1_000


@runtime_checkable
class QueueWrites(Protocol):
    """`QueueRepository`, as far as the queue rules need it to be."""

    def active(self) -> list[QueueItem]: ...

    def by_id(self, item_id: QueueItemId) -> QueueItem | None: ...

    def append(self, song_id: SongId, *, now: datetime | None = None) -> QueueItem: ...

    def set_status(
        self, item_id: QueueItemId, status: QueueItemStatus, *, at: datetime | None = None
    ) -> None: ...

    def remove(self, item_id: QueueItemId) -> bool: ...

    def clear(self) -> int: ...

    def compact(self) -> int: ...


@runtime_checkable
class HistoryWrites(Protocol):
    """The play log, written once per track that ended."""

    def record(
        self,
        *,
        song_id: SongId,
        started_at: datetime,
        finished_at: datetime | None = None,
        completion: float = 0.0,
        outcome: PlaybackOutcome | None = None,
    ) -> object: ...


class QueueTransaction(Protocol):
    """One transaction's worth of repositories. `UnitOfWork` is the implementation."""

    @property
    def queue(self) -> QueueWrites: ...

    @property
    def history(self) -> HistoryWrites: ...


class QueueStore(Protocol):
    """The persistence the queue needs, as a transaction factory.

    `RuntimeStore.unit_of_work` satisfies this, and the annotation in
    `tests/integration/test_queue_and_playback.py` is where mypy checks it — a protocol
    nobody assigns is a protocol nobody keeps.
    """

    @contextmanager
    def unit_of_work(self) -> Iterator[QueueTransaction]: ...


class SongLookup(Protocol):
    """Library reads: enough to accept a request and to name what is queued.

    `None` is a meaningful answer here rather than a failure: `library.db` is rebuilt and
    `runtime.db` outlives it (ADR-009), so a queued id can stop existing while the request
    stands. SAPRS 14.10 calls that "missing media files" and the queue's answer is to skip
    the item and keep playing, not to refuse the party.
    """

    def song(self, song_id: SongId) -> Song | None: ...

    def songs_by_ids(self, ids: Iterable[SongId]) -> list[Song]: ...


class CurrentTrack(Protocol):
    """The part of "what is playing" the queue is allowed to know."""

    @property
    def song_id(self) -> SongId: ...

    @property
    def queue_item_id(self) -> QueueItemId: ...


class Player(Protocol):
    """The playback capability (SAPRS 8.2's "begin playback").

    The one direct service-to-service dependency in Encore, justified in ADR-011: a queue
    request that begins playback is a *command*, and ADR-004 is explicit that events are
    facts that already happened. Everything going the other way — what ended, what
    recovered — is on the bus.
    """

    @property
    def is_idle(self) -> bool: ...

    @property
    def state(self) -> PlaybackState: ...

    @property
    def current(self) -> CurrentTrack | None: ...

    def play(self, song: Song, *, queue_item_id: QueueItemId) -> bool: ...

    def pause(self) -> bool: ...

    def resume(self) -> bool: ...

    def stop(self) -> None: ...

    def skip(self) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueEntry:
    """An item and, when the library still knows it, the song it names (SAPRS 9.9).

    Carrying the song rather than only the id is what keeps "Up Next" one batch read.
    `song=None` is the rebuilt-library case and must be rendered as an unavailable row,
    never dropped: a queue that quietly shortens itself in the UI is how a party loses
    track of what it is waiting for.
    """

    item: QueueItem
    song: Song | None = None

    @property
    def available(self) -> bool:
        return self.song is not None

    @property
    def position(self) -> int:
        return self.item.position


class QueueService:
    """Queue rules, positions and events (SAPRS Chapter 8).

    Args:
        store: The runtime database, as a transaction factory.
        library: Read-only song lookup. Never written to (AIG 4, ADR-006).
        player: The playback capability. See ADR-011 for why this is the one direct call.
        events: Where `SongQueued` and `QueueAdvanced` go, and where the service listens
            for `SongFinished` and `PlaybackRecovered`.
        config: `queue:` — the ceiling, and the duplicate rule that cannot be turned off.
        clock: Injectable, because positions and waiting times are timestamp arithmetic.
        logger: For the skips a guest never sees and an operator sometimes needs.
    """

    def __init__(
        self,
        *,
        store: QueueStore,
        library: SongLookup,
        player: Player,
        events: EventBus,
        config: QueueConfig | None = None,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._library = library
        self._player = player
        self._events = events
        self._config = config if config is not None else QueueConfig()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._logger = logger or logging.getLogger("encore.queue")
        self._advancing = False
        self._subscriptions: list[Subscription] = []

    # -- lifecycle --------------------------------------------------------

    def start(self) -> int:
        """Subscribe, and bring back whatever a restart left queued (SAPRS 8.5, 8.6).

        An item recorded as `PLAYING` by a process that is no longer running becomes
        `PENDING` at the head: it was not finished, nobody skipped it, and the appliance is
        silent. Nothing starts playing from here — a server that began making noise before
        a guest reached the page would be answering to someone's hallway, not to a request.
        """

        if not self._subscriptions:
            self._subscriptions = [
                self._events.subscribe(SongFinished, self._on_song_finished, name="queue.advance"),
                self._events.subscribe(
                    PlaybackRecovered, self._on_recovered, name="queue.resume_after_recovery"
                ),
            ]
        restored = 0
        with self._store.unit_of_work() as work:
            for item in work.queue.active():
                if item.status is QueueItemStatus.PLAYING:
                    work.queue.set_status(item.id, QueueItemStatus.PENDING, at=self._clock.now())
                    restored += 1
            work.queue.compact()
        if restored:
            self._logger.info("restored queue after restart", extra={"items": restored})
        return restored

    def close(self) -> None:
        """Withdraw the subscriptions. Idempotent, and called from the shutdown path."""

        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions = []

    # -- the guest's operation (SAPRS 8.2) -------------------------------

    def enqueue(self, song_id: SongId) -> QueueItem:
        """Accept one request. Returns the item that now exists.

        Raises:
            SongNotAvailableError: The library has no such song — 404, not 409.
            QueueFullError: `queue.max_items` is reached; the limit is in the message.
        """

        if self._library.song(song_id) is None:
            raise SongNotAvailableError(song_id)
        now = self._clock.now()
        with self._store.unit_of_work() as work:
            waiting = len(work.queue.active())
            if waiting >= self._config.max_items:
                raise QueueFullError(self._config.max_items)
            item = work.queue.append(song_id, now=now)
            length = waiting + 1
        self._events.publish(
            SongQueued(
                song_id=item.song_id,
                queue_item_id=item.id,
                position=item.position,
                queue_length=length,
                occurred_at=now,
            )
        )
        if self._player.is_idle:
            self._advance()
            # The caller is answering a guest, and "queued" and "playing" are different
            # sentences on the screen. Re-reading costs one query and reports the truth.
            return self._item(item.id) or item
        return item

    # -- reading (SAPRS 9.9, 10.3) ----------------------------------------

    @property
    def length(self) -> int:
        """Items still to play, including the one on the speakers."""

        with self._store.unit_of_work() as work:
            return len(work.queue.active())

    def active(self) -> list[QueueItem]:
        with self._store.unit_of_work() as work:
            return work.queue.active()

    def up_next(self, *, limit: int | None = None) -> list[QueueEntry]:
        """What is waiting, in order, with the songs resolved in one read (SAPRS 9.9).

        Excludes the current item: "Up Next" and "Now Playing" are two headings on one
        screen, and repeating a track across both is the kind of detail that makes a queue
        look wrong when it is only being accurate.
        """

        entries = self._entries(self.active())
        waiting = [entry for entry in entries if entry.item.status is QueueItemStatus.PENDING]
        return waiting if limit is None else waiting[:limit]

    def now_playing(self) -> QueueEntry | None:
        """The item the engine is playing, or None.

        Read from the queue rather than from the player, so the answer is the same one the
        database gives and the same one `up_next` excludes.
        """

        current = self._player.current
        if current is None:
            return None
        item = self._item(current.queue_item_id)
        if item is None:
            return None
        return QueueEntry(item=item, song=self._library.song(item.song_id))

    def entry(self, item_id: QueueItemId) -> QueueEntry | None:
        item = self._item(item_id)
        if item is None:
            return None
        return QueueEntry(item=item, song=self._library.song(item.song_id))

    def wait_time(self, *, now: datetime | None = None) -> timedelta:
        """How long the front of the queue has been waiting (SAPRS 8.4's visible queue).

        The empty queue answers zero rather than raising, because "0:00" and "no data" are
        the same sentence to a volunteer running the event.
        """

        moment = now if now is not None else self._clock.now()
        pending = [item for item in self.active() if item.status is QueueItemStatus.PENDING]
        if not pending:
            return timedelta(0)
        return max(timedelta(0), moment - min(item.enqueued_at for item in pending))

    # -- administrative controls (SAPRS 8.8) ------------------------------

    def skip(self) -> None:
        """Skip the current track. The advance itself is the event's business.

        Deliberately thin: asking the player to skip publishes `SongFinished(SKIPPED)`, and
        the handler settles the item, records the outcome and selects the next one — which
        is the whole of 8.7 run once. Duplicating any of that here would be a second path
        through the same rules, and second paths are where they disagree.
        """

        self._player.skip()

    def stop(self) -> None:
        """Stop and hold. The current item returns to the head (see `SETTLEMENT`)."""

        self._player.stop()

    def pause(self) -> bool:
        return self._player.pause()

    def resume(self) -> bool:
        return self._player.resume()

    def play_next(self) -> bool:
        """Begin the head item on an idle appliance (the admin "start" button).

        Returns whether anything is now playing. An empty queue is not an error.
        """

        if not self._player.is_idle:
            return False
        self._advance()
        return not self._player.is_idle

    def remove(self, item_id: QueueItemId) -> bool:
        """Drop one item, reporting whether it was there.

        Removing the *current* item is a skip, because the alternative is a silent
        appliance with a queue that looks stuck: an administrator who takes out what is
        playing wants the next song, not an empty second. Either way the remaining items
        keep their relative order, which is 8.8's last sentence.
        """

        item = self._item(item_id)
        if item is None:
            return False
        if item.status is QueueItemStatus.PLAYING:
            self._player.skip()
            return True
        with self._store.unit_of_work() as work:
            work.queue.set_status(item_id, QueueItemStatus.REMOVED, at=self._clock.now())
            work.queue.compact()
            remaining = len(work.queue.active())
        self._publish_advanced(remaining=remaining, finished=item_id)
        return True

    def clear(self) -> int:
        """Take the waiting requests out. The current track is left alone.

        SAPRS 8.8 lists removal as an administrative operation on the queue, not on the
        amplifier: "clear" means "stop taking requests", and an admin panel that can cut
        the music by accident is a worse instrument than one that cannot.
        """

        with self._store.unit_of_work() as work:
            removed = work.queue.clear()
            work.queue.compact()
            remaining = len(work.queue.active())
        current = self._player.current
        self._publish_advanced(
            remaining=remaining,
            finished=None,
            now_playing=None if current is None else current.song_id,
        )
        return removed

    # -- the advance (SAPRS 8.7) ------------------------------------------

    def _on_song_finished(self, event: SongFinished) -> None:
        """Steps 2-5 of SAPRS 8.7: mark it, record it, choose next, begin it.

        Step 1, the completion event, is what called this. Running the rest inline is what
        ADR-004's synchronous dispatch is for, and it is also what keeps a queue advancing
        when the process has no event loop running — the case a jukebox hits at the worst
        moment of the night.
        """

        self._settle(event)
        if event.reason is FinishedReason.STOPPED:
            # A stop is a hold, not an ending: the item is back at the head and nothing
            # starts until a guest queues something or an admin presses play. Advancing here
            # would make the stop button play the same song again, which is not what the word
            # means to anyone standing in the room.
            return
        if self._advancing:
            # Re-entered from inside `player.play()` because the file was unreadable. The
            # outer loop is about to look at the queue again, and a second loop nested
            # inside the first would nest one frame per broken file until the Event Bus
            # called the wiring a cycle.
            return
        self._advance()

    def _on_recovered(self, event: PlaybackRecovered) -> None:
        """Continue queue processing after mpv came back (SAPRS 7.6 step 7).

        The only thing the queue needs from a recovery is that the engine is there again;
        which track that means is the head item's business, and 8.5 says the head is chosen
        by arrival order, not by what was interrupted.
        """

        if not self._player.is_idle:
            return
        self._logger.info(
            "continuing queue after playback recovery",
            extra={"restart_count": event.restart_count},
        )
        self._advance()

    def _settle(self, event: SongFinished) -> None:
        """Mark the item that ended and write the play log, in one transaction.

        Idempotent by the item's own status: only a `PLAYING` item can be settled, so the
        double publication this cannot fully prevent — a command racing the progress tick —
        costs one ignored call rather than two history rows. A queue whose statistics
        double-count a song is a queue nobody trusts.
        """

        with self._store.unit_of_work() as work:
            item = work.queue.by_id(event.queue_item_id)
            if item is None or item.status is not QueueItemStatus.PLAYING:
                return
            work.queue.set_status(item.id, SETTLEMENT[event.reason], at=event.occurred_at)
            work.history.record(
                song_id=event.song_id,
                started_at=item.played_at or event.occurred_at,
                finished_at=event.occurred_at,
                completion=event.completion,
                outcome=event.reason.outcome,
            )
            work.queue.compact()

    def _advance(self) -> None:
        """Walk the head forward until something is audible or the queue is empty.

        The loop rather than recursion is the point: twenty files the appliance cannot
        decode would otherwise nest one `SongFinished` inside another, sixty-four deep,
        until `EventBus` raised `EventCycleError` and the party heard one error instead of
        twenty.
        """

        self._advancing = True
        try:
            for _ in range(_MAX_WALK):
                if not self._play_head():
                    return
        finally:
            self._advancing = False
        self._logger.error(
            "queue advance stopped walking",
            extra={"limit": _MAX_WALK, "queue_length": self.length},
        )

    def _play_head(self) -> bool:
        """Try to begin the first waiting item. True means "ask again".

        False means the loop is done: either something is now playing, or there is nothing
        left to try. Every branch that returns True has either consumed an item or left the
        queue shorter than it was, which is what makes the bounded loop terminate.
        """

        if not _is_idle(self._player):
            # An engine in `Error` is neither idle nor playing, and handing it a track would
            # raise an illegal transition. The advance stops here and `PlaybackRecovered`
            # starts it again, which is why recovery is an event and not a retry loop.
            return False
        head = self._head()
        if head is None:
            return False
        song = self._library.song(head.song_id)
        if song is None:
            self._logger.warning(
                "skipping a queued song that is no longer in the library",
                extra={"queue_item_id": int(head.id), "song_id": int(head.song_id)},
            )
            with self._store.unit_of_work() as work:
                work.queue.set_status(head.id, QueueItemStatus.REMOVED, at=self._clock.now())
                work.queue.compact()
            return True
        self._publish_advanced(remaining=len(self.active()), finished=None, now_playing=song.id)
        # `Playing` is recorded before the engine is asked, not after it answers. The order
        # is what ends a queue full of unreadable files: `SongFinished(FAILED)` is published
        # from inside `play()`, its handler settles only an item that was actually current,
        # and an item nobody had marked current would stay `Pending` forever — the advance
        # would retry it, and retry it, until the walk limit stopped it.
        with self._store.unit_of_work() as work:
            work.queue.set_status(head.id, QueueItemStatus.PLAYING, at=self._clock.now())
        started = self._player.play(song, queue_item_id=head.id)
        if not started:
            self._abandon(head.id)
            return True
        # Still playing means the loop is done. Idle means it ended again before we were
        # back — an empty or silent file — whose row the handler has already settled, so the
        # next head is what matters now.
        return _is_idle(self._player)

    def _abandon(self, item_id: QueueItemId) -> None:
        """Take an item out that the engine refused without reporting it.

        Belt and braces: `PlaybackService.play()` publishes `SongFinished(FAILED)` on every
        path that returns False, and if a future engine ever returns False silently, the
        alternative is a `Playing` row that blocks the queue with no event to unblock it.
        """

        with self._store.unit_of_work() as work:
            item = work.queue.by_id(item_id)
            if item is not None and item.status is QueueItemStatus.PLAYING:
                self._logger.error(
                    "the engine refused a track without reporting it",
                    extra={"queue_item_id": int(item_id)},
                )
                work.queue.set_status(item_id, QueueItemStatus.REMOVED, at=self._clock.now())
                work.queue.compact()

    # -- internals --------------------------------------------------------

    def _head(self) -> QueueItem | None:
        """The first waiting item, or None. Position order, never arrival by clock."""

        pending = [item for item in self.active() if item.status is QueueItemStatus.PENDING]
        return min(pending, key=lambda item: item.position, default=None)

    def _item(self, item_id: QueueItemId) -> QueueItem | None:
        with self._store.unit_of_work() as work:
            return work.queue.by_id(item_id)

    def _entries(self, items: Sequence[QueueItem]) -> list[QueueEntry]:
        songs = {
            song.id: song
            for song in self._library.songs_by_ids(_unique(item.song_id for item in items))
        }
        return [QueueEntry(item=item, song=songs.get(item.song_id)) for item in items]

    def _publish_advanced(
        self,
        *,
        remaining: int,
        finished: QueueItemId | None,
        now_playing: SongId | None = None,
    ) -> None:
        """Report that the FIFO moved (SAPRS 8.9).

        Silence is reported here and sound is reported by `SongStarted`: the interface needs
        both facts and they are not the same fact, and a UI that waits for the audio to
        start before it redraws "Up Next" is a UI that freezes on a broken file.
        """

        self._events.publish(
            QueueAdvanced(
                finished_queue_item_id=finished,
                now_playing=now_playing,
                queue_length=remaining,
                occurred_at=self._clock.now(),
            )
        )


def _is_idle(player: Player) -> bool:
    """Whether the engine will take a track.

    A function rather than the property, because `player.play()` changes the answer and a
    type checker that saw one read of `is_idle` at the top of a function would conclude the
    second read downstream was impossible. That is exactly the reasoning a queue advance
    needs the checker *not* to make.
    """

    return player.is_idle


def _unique(ids: Iterable[SongId]) -> list[SongId]:
    """De-duplicate, ordered. `IN ()` is a syntax error and a repeat is wasted work."""

    seen: dict[int, SongId] = {}
    for song_id in ids:
        seen.setdefault(int(song_id), song_id)
    return [seen[key] for key in sorted(seen)]
