"""The Playback Service: the only component that tells mpv anything (SAPRS 7.1, 7.9).

Commands in, facts out. `play`, `pause`, `resume`, `stop`, `skip`, `seek` and `load` are
SAPRS 7.4's list; `SongStarted`, `SongFinished` and `PlaybackProgress` are what everyone
else is allowed to know. Nothing here imports HTTP, SQLite, the library or the queue
(SAPRS 11.10), and the one thing it asks of an outside object is a restart, through
`EngineRecovery`.

The state machine is the diagram in SAPRS 7.3, imported as data from
`encore.domain.playback` and enforced on every change: an edge the diagram does not draw
raises `IllegalTransitionError` rather than being logged. That is stricter than "state
is observable" requires, and it pays for itself the first time a recovery path lands in a
state nobody drew.

**One publication per track, after the silence.** `SongFinished` names the track that
ended and clears it, so a command racing the progress tick cannot report the same track
twice. It is published *after* the machine has reached `Idle`, deliberately and for one
reason: the queue's handler for that event starts the next track, and an event published
from `Playing` asks the machine to transition to itself. `ADR-011` records the ordering
rule; this paragraph is where it is kept.

**The reasons divide by who caused them.** The engine's own observations produce
`COMPLETED` and `FAILED`; the commands produce `SKIPPED` and `STOPPED`. That split is how
`QueueService` tells "the party finished this song" from "someone pressed stop" without
reading anything but the event (SAPRS 8.7, 8.8).

**Why `tick()` exists.** mpv is asynchronous and a JSON IPC socket is not a callback:
something has to ask. `tick()` is that asking, about once a second (SAPRS 7.5), and it is
the only place the service discovers that a track ended on its own. Whoever owns the clock
— the server loop in milestone 11 — calls it. Nothing here starts a thread, which keeps
every test in this area deterministic and keeps `docs/Developer/Testing.md`'s ban on
wall-clock timing in unit tests honest.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from encore.domain import (
    PlaybackProgress,
    PlaybackState,
    QueueItemId,
    Song,
    SongId,
    can_transition,
)
from encore.events.bus import EventBus
from encore.events.playback import FinishedReason, SongFinished, SongStarted
from encore.playback.errors import (
    IllegalTransitionError,
    MpvCommandError,
    MpvGoneError,
    MpvTimeoutError,
)
from encore.playback.player import MpvPlayer
from encore.playback.transition import TransitionPolicy
from encore.utilities.clock import Clock, SystemClock, ensure_aware

__all__ = ["COMPLETION_CEILING", "EngineRecovery", "PlaybackService", "PlayingTrack"]

#: How far past a declared length still counts as "played to the end". mpv reports a
#: position slightly beyond a truncated file's duration, and a completion of 1.004 would
#: otherwise read as a bug in the statistics rather than in the container.
COMPLETION_CEILING = 1.0

#: The engine failures that mean "this did not happen". Collected as a constant because
#: the distinction that matters is a refused command against an absent engine, and every
#: handler in this module has to make it the same way.
_ENGINE_FAILURE = (MpvCommandError, MpvGoneError, MpvTimeoutError)


class EngineRecovery(Protocol):
    """The restart capability, named (SAPRS 7.2, 7.6).

    The service detects; the supervisor restarts. Declared as one method so
    `PlaybackService` is testable without a process, and so AIG 4's "services communicate
    through interfaces" holds of the one dependency that crosses a package boundary
    inside `encore/playback/`.
    """

    def recover(self, *, reason: str) -> bool: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class PlayingTrack:
    """What is on the speakers, and since when.

    Attributes:
        song: The library entity, handed to playback by the queue. Playback never opens
            `library.db` (SAPRS 7.9): the path arrives as an argument.
        queue_item_id: The request that caused it (SAPRS 4.5). Required, because
            everything played was asked for by someone, and the events that report a track
            name the request rather than the track.
        started_at: When the engine took the file — the timestamp history wants and the
            denominator of `completion`.
    """

    song: Song
    queue_item_id: QueueItemId
    started_at: datetime

    def __post_init__(self) -> None:
        ensure_aware(self.started_at, field_name="started_at")

    @property
    def song_id(self) -> SongId:
        return self.song.id


class PlaybackService:
    """mpv's commands, Encore's state machine, and nothing else (SAPRS Chapter 7).

    Args:
        player: The engine vocabulary, built over the supervisor's channel so a restart is
            invisible to it.
        events: Where the facts go. Never bypassed (AIG 22).
        policy: Gapless and crossfade, from `audio:`.
        clock: Injectable, because `started_at` and every completion ratio is arithmetic.
        recovery: The supervisor, when there is one. Absent means "nobody will restart
            this engine", which is how the service is tested alone.
        logger: For the service's own reporting; failures are events as well as logs.
    """

    def __init__(
        self,
        *,
        player: MpvPlayer,
        events: EventBus,
        policy: TransitionPolicy | None = None,
        clock: Clock | None = None,
        recovery: EngineRecovery | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._player = player
        self._events = events
        self._policy = policy if policy is not None else TransitionPolicy()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._recovery = recovery
        self._logger = logger or logging.getLogger("encore.playback")
        self._state = PlaybackState.IDLE
        self._current: PlayingTrack | None = None
        self._progress = PlaybackProgress(state=PlaybackState.IDLE)
        self._fading = False

    # -- what is true now -------------------------------------------------

    @property
    def state(self) -> PlaybackState:
        return self._state

    @property
    def current(self) -> PlayingTrack | None:
        return self._current

    @property
    def is_idle(self) -> bool:
        """Whether the appliance would start playing something the moment it is asked.

        SAPRS 8.2's branch as one question: `IDLE` and `FINISHED` — the latter because it
        is the truth between a track ending and the queue advancing. A `PAUSED` appliance
        answers "no", which is what makes a queued request land in Up Next rather than
        interrupt whoever pressed pause.
        """

        return self._state in {PlaybackState.IDLE, PlaybackState.FINISHED}

    @property
    def progress(self) -> PlaybackProgress:
        """The last observation. Never touches the engine.

        A property rather than a polling method so that `/now-playing` in milestone 11
        cannot multiply mpv reads by multiplying requests.
        """

        return self._progress

    # -- commands (SAPRS 7.4) ---------------------------------------------

    def play(self, song: Song, *, queue_item_id: QueueItemId, paused: bool = False) -> bool:
        """Load `song` and begin. Returns whether the engine accepted it.

        Loads, then observes once. With `MockMpv` the answer is already there; with mpv
        `loadfile` has merely been accepted, so the `Loading → Playing` edge and
        `SongStarted` arrive on the next tick. Either way there is one publication and it
        happens when sound starts, not when it was asked for (SAPRS 8.7).

        A refusal publishes `SongFinished(FAILED)` before returning False (SAPRS 11.9: a
        missing file is reported, not swallowed), which is how a queue of broken files
        walks itself instead of stopping at the first.
        """

        self._leave_finished()
        self._set_state(PlaybackState.LOADING)
        self._current = PlayingTrack(
            song=song, queue_item_id=queue_item_id, started_at=self._clock.now()
        )
        try:
            self._player.load(song.file_path, paused=paused)
        except _ENGINE_FAILURE as error:
            self._logger.warning(
                "could not load track",
                extra={
                    "song_id": int(song.id),
                    "file": str(song.file_path),
                    "error_type": type(error).__name__,
                },
            )
            self._end(FinishedReason.FAILED, completion=0.0)
            return False
        if not paused:
            self._observe(announcing=True)
        return True

    def load(self, song: Song, *, queue_item_id: QueueItemId) -> bool:
        """SAPRS 7.4's separate "load track": cued, and silent.

        Present because the diagram has a `Loading` state and the admin interface has a
        "cue this" affordance; implementing one without the other is how a jukebox makes
        noise nobody asked for.
        """

        return self.play(song, queue_item_id=queue_item_id, paused=True)

    def pause(self) -> bool:
        """Silence without forgetting where the track was.

        Returns False, having changed nothing, when there is nothing to pause. The admin
        surface offers pause on a machine that may already be idle, and a state error at a
        button press is worse than a no-op the SSE stream corrects within a second.
        """

        if self._state is not PlaybackState.PLAYING:
            return False
        self._player.set_paused(True)
        self._set_state(PlaybackState.PAUSED)
        self._publish_state()
        return True

    def resume(self) -> bool:
        """Un-pause, or begin a track that was only cued.

        `LOADING` is accepted for the second case: `load` parks the machine in it, and the
        diagram's only way out besides an error is `Playing`.
        """

        if self._state is PlaybackState.PAUSED:
            self._player.set_paused(False)
            self._set_state(PlaybackState.PLAYING)
            self._publish_state()
            return True
        if self._state is PlaybackState.LOADING:
            self._player.set_paused(False)
            self._observe(announcing=True)
            return True
        return False

    def stop(self) -> None:
        """End the current track and hold (SAPRS 8.8's "stop playback").

        What "hold" means for the queued items is the queue's decision (SAPRS 8.5); this
        reports the fact as `STOPPED`, which is how the queue tells it apart from a
        completion and a skip.
        """

        self._end(FinishedReason.STOPPED)

    def skip(self) -> None:
        """End the current track because the party moved on (SAPRS 8.8)."""

        self._end(FinishedReason.SKIPPED)

    def seek(self, position: timedelta) -> bool:
        """Jump within the current track, where appropriate (SAPRS 7.4).

        "Where appropriate" is a state question and not a preference: seeking is refused
        while loading, when there is no decoded position to change, and while idle.
        """

        if self._state not in {PlaybackState.PLAYING, PlaybackState.PAUSED}:
            return False
        self._player.seek_to(position)
        self._progress = self._snapshot(position=position)
        self._apply_fade(position, self._progress.duration)
        return True

    # -- the 1 Hz observation (SAPRS 7.5) -------------------------------

    def tick(self) -> PlaybackProgress:
        """Ask mpv what it is doing, publish what changed, return the progress.

        The only place a track is discovered to have ended by itself, and so the only place
        `COMPLETED` is published.
        """

        return self._observe()

    def engine_lost(self, reason: str) -> None:
        """Called by the supervisor when mpv died (SAPRS 7.6 steps 1-3).

        The current track becomes a `FAILED` fact and the machine goes to `ERROR`;
        restarting is not this method's job. The separation is what makes one crash publish
        one truth rather than three partial ones.
        """

        if self._state is PlaybackState.ERROR:
            # Already reported: the fact is out, and publishing it twice would settle the
            # queue item twice. The restart is still worth asking for, because the attempt
            # that put us here may have been the one that never ran.
            if self._recovery is not None:
                self._recovery.recover(reason=reason)
            return
        self._logger.warning("playback engine lost", extra={"reason": reason})
        track, self._current = self._current, None
        ratio = self._completion()
        self._fading = False
        # The crash goes to `Error` first rather than straight to idle, because that is
        # the edge SAPRS 7.6 draws and because `recovered()` needs somewhere to come back
        # from. The queue's handler for the event below must therefore not treat an error
        # state as "idle" — which is why `is_idle` names the two states it means.
        if can_transition(self._state, PlaybackState.ERROR):
            self._set_state(PlaybackState.ERROR)
        else:
            self._to_idle()
        self._progress = self._snapshot()
        if track is not None:
            self._events.publish(
                SongFinished(
                    song_id=track.song_id,
                    queue_item_id=track.queue_item_id,
                    reason=FinishedReason.FAILED,
                    completion=min(max(ratio, 0.0), COMPLETION_CEILING),
                    occurred_at=self._clock.now(),
                )
            )
        if self._recovery is not None:
            self._recovery.recover(reason=reason)

    def recovered(self, *, resumed: PlaybackState = PlaybackState.IDLE) -> None:
        """The engine is back; come through the diagram's recovery edge (SAPRS 7.6)."""

        if self._state is not PlaybackState.ERROR:
            self._to_idle()
            return
        self._set_state(PlaybackState.RECOVERING)
        self._set_state(resumed if resumed is PlaybackState.PLAYING else PlaybackState.IDLE)

    # -- internals -------------------------------------------------------

    def _observe(self, *, announcing: bool = False) -> PlaybackProgress:
        """One reading of the engine, and the transitions it implies.

        `announcing` is `play`'s fast path: the same code, allowed to conclude "playing"
        from a load mpv has merely accepted, because a caller is standing there.
        """

        try:
            observation = self._player.observe()
        except MpvGoneError as error:
            self.engine_lost(f"{type(error).__name__}: {error}")
            return self._progress
        except MpvTimeoutError as error:
            self._logger.warning("mpv did not answer", extra={"detail": str(error)})
            return self._progress
        except MpvCommandError as error:  # pragma: no cover - a refused property read
            self._logger.debug("mpv refused a property read", extra={"detail": str(error)})
            return self._progress

        if self._current is None:
            # Nothing was asked for, so nothing can have ended. mpv sitting idle with no
            # file is the appliance between requests, not a fact worth publishing.
            self._progress = self._snapshot()
            return self._progress

        if observation.finished or observation.state is PlaybackState.IDLE:
            if self._state is PlaybackState.PLAYING:
                # The diagram's own edge, taken before the report: a track that ran out is
                # `Finished`, not `Stopping`, and the difference is what an operator
                # reading a log at 2 a.m. is looking for.
                self._set_state(PlaybackState.FINISHED)
            self._end(FinishedReason.COMPLETED, completion=1.0, silence=False)
            return self._progress

        if (
            self._state is PlaybackState.LOADING
            and observation.state is not PlaybackState.PAUSED
            and (announcing or observation.has_file)
        ):
            self._set_state(PlaybackState.PLAYING)
            self._announce()

        self._progress = self._snapshot(
            position=observation.position, duration=observation.duration
        )
        self._apply_fade(observation.position, observation.duration)
        return self._progress

    def _end(
        self,
        reason: FinishedReason,
        *,
        completion: float | None = None,
        silence: bool = True,
    ) -> None:
        """Stop reporting a track, silence the engine if one is running, publish once.

        `silence=False` is for the paths where the engine has already stopped by itself
        (end of file) or is no longer there (crash): telling a dead mpv to `stop` is an
        `MpvGoneError` inside a failure handler, which is how one fault becomes two.
        """

        ratio = self._completion() if completion is None else completion
        track, self._current = self._current, None
        self._fading = False
        self._to_idle(silence=self._player.stop if silence else None)
        self._restore_volume()
        self._progress = self._snapshot()
        if track is None:
            return
        self._events.publish(
            SongFinished(
                song_id=track.song_id,
                queue_item_id=track.queue_item_id,
                reason=reason,
                completion=min(max(ratio, 0.0), COMPLETION_CEILING),
                occurred_at=self._clock.now(),
            )
        )

    def _announce(self) -> None:
        """Report that sound began — SAPRS 7.3's `Loading → Playing` edge."""

        track = self._current
        if track is None:  # pragma: no cover - the caller holds the track
            return
        self._events.publish(
            SongStarted(
                song_id=track.song_id,
                queue_item_id=track.queue_item_id,
                occurred_at=self._clock.now(),
            )
        )

    def _completion(self) -> float:
        """How much of the current track was heard, from the last observation.

        Duration comes from the reading rather than from `Song.duration`, so a file whose
        declared and decoded lengths disagree is measured against what was played.
        """

        if self._current is None or self._progress.duration <= timedelta(0):
            return 0.0
        return self._progress.position.total_seconds() / self._progress.duration.total_seconds()

    def _snapshot(
        self,
        *,
        position: timedelta | None = None,
        duration: timedelta | None = None,
    ) -> PlaybackProgress:
        """The progress value that is true right now, from state and last reading.

        One constructor for every place `_progress` is assigned, because the three fields
        that are not re-read have to be carried forward deliberately and a missed one reads
        as a track that never moves.
        """

        return PlaybackProgress(
            state=self._state,
            song_id=None if self._current is None else self._current.song_id,
            position=self._progress.position if position is None else position,
            duration=self._progress.duration if duration is None else duration,
        )

    def _apply_fade(self, position: timedelta, duration: timedelta) -> None:
        """Set the volume the transition policy wants at this moment, if it wants one."""

        if not self._policy.fading:
            return
        volume = self._policy.volume_for(position, duration)
        if volume is None:
            if self._fading:
                self._restore_volume()
            return
        _try(lambda: self._player.set_volume(volume), self._logger)
        self._fading = True

    def _restore_volume(self) -> None:
        if not self._policy.fading:
            return
        _try(self._player.restore_volume, self._logger)
        self._fading = False

    def _publish_state(self) -> None:
        """Refresh the reported state without reading mpv.

        A pause is true the instant the command is accepted; waiting for the next tick to
        say so would show a party a progress bar that is still moving.
        """

        self._progress = self._snapshot()

    def _to_idle(self, *, silence: Callable[[], None] | None = None) -> None:
        """Reach `Idle` along legal edges from wherever the machine is, silencing at `Stopping`.

        The failure paths arrive at this method from states their callers did not choose —
        a crash while paused, a stop while already idle — and the diagram says each of them
        leaves by a different door. The `silence` command is issued at `STOPPING` rather
        than before or after, because that is what the state means.
        """

        if self._state in {PlaybackState.ERROR, PlaybackState.RECOVERING}:
            self._set_state(PlaybackState.IDLE)
            return
        if self._state is PlaybackState.FINISHED:
            self._set_state(PlaybackState.IDLE)
            return
        if self._state is PlaybackState.IDLE:
            if silence is not None:
                _try(silence, self._logger)
            return
        self._set_state(PlaybackState.STOPPING)
        if silence is not None:
            _try(silence, self._logger)
        self._set_state(PlaybackState.IDLE)

    def _leave_finished(self) -> None:
        """Take the diagram's only edge out of `Finished`, when that is where we are.

        `Finished` has exactly one outgoing edge, to `Idle`. Normalising it here means the
        commands can assume they start from a state their own edges describe, instead of
        every one of them carrying a special case for a half-second window.
        """

        if self._state is PlaybackState.FINISHED:
            self._set_state(PlaybackState.IDLE)

    def _set_state(self, following: PlaybackState) -> None:
        if not can_transition(self._state, following):
            raise IllegalTransitionError(self._state, following)
        self._state = following


def _try(action: Callable[[], None], logger: logging.Logger) -> None:
    """Do something to the engine that is fine to fail.

    `stop` on an mpv that has already gone is not a fault in the shutdown path — the
    process is, in fact, stopped — and letting it escape would turn SAPRS 11.8's "exit
    cleanly" into an exception from a `finally`.
    """

    try:
        action()
    except _ENGINE_FAILURE as error:
        logger.debug("engine command failed during teardown", extra={"detail": str(error)})
