"""Regression: #48 — mpv refuses every file, and the appliance reports itself healthy.

The mechanism is not mpv's refusal. It is what Encore compared that refusal against.

On a Raspberry Pi 4 with nothing plugged into its HDMI port, `audio.device: default` names card
0, which is that port, and ALSA cannot open a card with no sink behind it. mpv accepts `loadfile`,
fails to initialise an audio output, and pushes `end-file reason=error
file_error="audio output initialization failed"` — the same reply it gives for a file it cannot
demux, and the same reply it gives for a PulseAudio or PipeWire server the service account cannot
reach. The appliance's answer, in all three cases, was one WARNING naming whichever track a guest
had most recently requested:

```text
{"level":"WARNING","logger":"encore.playback","message":"mpv refused the file","song_id":900,
 "file":"/opt/music/.../04_Bad_Moon_Rising.m4a","detail":"audio output initialization failed"}
```

and `/api/v1/health` said `{"component":"playback","status":"healthy","detail":"mpv running as
pid 503668"}` — true, and beside the point. The queue walked itself to empty against a silent
room for an hour, in both mp3 and m4a, because nothing in Encore distinguished *"this song* is
unplayable" from *"this machine* cannot make sound".

The reproduction is two commands, and the second one is why this file does not need a broken
speaker to be honest:

```console
$ sudo -u encore mpv -v --ao=alsa --audio-device=default  <any file in the library>
[ao/alsa] opening device 'default'
[ao/alsa] Playback open error: Unknown error 524
[cplayer] finished playback, audio output initialization failed (reason 4)
$ sudo -u encore mpv -v --ao=alsa --audio-device=alsa/plughw:CARD=Headphones,DEV=0 <same file>
[cplayer] finished playback, success (reason 0)
```

Three things had to change, and one line here stands for each: the refusal is *classified*
(`PlaybackFault`, `encore/playback/fault.py`), the classification reaches the component that owns
the appliance's health (`PlaybackSupervisor.health()`), and the file the operator would have had
to edit says where the value comes from (`apps/installer/content.py`, tested in
`tests/unit/test_installer_content.py`). What did *not* change is the event: `SongFinished(FAILED)`
is still correct per track, because the queue is right to move on either way — that half is
asserted below too, since a fix that made a broken file block a party would be worse than the bug.

`tests/integration/test_real_mpv.py` is the companion: it makes a real mpv refuse a real device and
checks the sentence it used, so this file's strings cannot drift from the engine's.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from encore.config.models import PlaybackConfig
from encore.domain import (
    AlbumId,
    ArtistId,
    AudioFormat,
    HealthStatus,
    PlaybackState,
    QueueItemId,
    Song,
    SongId,
)
from encore.events import EventBus, SongFinished, SongStarted
from encore.events.playback import FinishedReason
from encore.playback import MpvPlayer, PlaybackService, PlaybackSupervisor
from encore.playback.fault import FaultKind
from encore.playback.ipc import CommandChannel
from encore.services.health_service import HealthService
from tests.support.mpv import AUDIO_OUTPUT_FAILURE, MockMpv

pytestmark = pytest.mark.regression

START = datetime(2026, 10, 29, 22, 46, tzinfo=UTC)

#: The three tracks the issue reporter asked for, in the order the journal shows them. Two mp4a,
#: one mp3: the format was never the variable, and a test that used one file could not say so.
REQUESTS = ((900, "Bad Moon Rising.m4a"), (901, "Sinister Purpose.mp3"), (168, "Bad Company.mp3"))


class FixedClock:
    def __init__(self) -> None:
        self.at = START

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class RecordingBus(EventBus):
    """The real bus, with the facts kept, so ordering is the bus's and not a fake's."""

    def __init__(self) -> None:
        super().__init__(logger=logging.getLogger("encore.test.issue48"))
        self.published: list[object] = []

    def publish(self, event: object) -> object:  # type: ignore[override]
        self.published.append(event)
        return super().publish(event)  # type: ignore[arg-type]


@dataclass
class RunningLauncher:
    """An mpv that is alive, answers IPC, and cannot make sound.

    That arrangement is the whole of #48: every prior test of this path had an engine that either
    died (and the supervisor already handled that) or worked.
    """

    mpv: MockMpv
    launches: int = 1

    def launch(self) -> CommandChannel:
        """Bring an engine up. Alive, answering IPC, and still unable to make sound."""

        self.launches += 1
        self.mpv.running = True
        return self.mpv

    def terminate(self) -> None:
        self.mpv.running = False

    @property
    def alive(self) -> bool:
        return self.mpv.running

    def diagnostics(self) -> str:
        return f"mpv running as pid {400000 + self.launches}"


@dataclass
class Appliance:
    """Player, service, monitor and the aggregate they report into.

    Assembled rather than imported from `apps/server`, because the server is not what was wrong
    and this file must not need an HTTP stack to say what was.
    """

    mpv: MockMpv = field(default_factory=MockMpv)
    clock: FixedClock = field(default_factory=FixedClock)

    def __post_init__(self) -> None:
        self.events = RecordingBus()
        self.supervisor = PlaybackSupervisor(
            launcher=RunningLauncher(self.mpv),
            events=self.events,
            config=PlaybackConfig(restart_backoff_seconds=[1.0]),
            clock=self.clock,
            logger=logging.getLogger("encore.test.issue48.supervisor"),
        )
        self.playback = PlaybackService(
            player=MpvPlayer(self.supervisor.channel, volume=85.0),
            events=self.events,
            clock=self.clock,
            recovery=self.supervisor,
            logger=logging.getLogger("encore.playback"),
        )
        self.supervisor.bind(self.playback)
        self.health = HealthService(
            sources=[("playback", self.supervisor)],
            events=self.events,
            clock=self.clock,
        )

    def request(self, song_id: int, title: str, *, detail: str) -> None:
        """Ask for one track, and let the engine answer the way it just did."""

        self.mpv.refuse_next_load(detail)
        self.playback.play(_song(song_id, title), queue_item_id=QueueItemId(song_id))
        self.clock.advance(1.0)
        self.playback.tick()

    def events_of(self, kind: type[object]) -> list[object]:
        return [event for event in self.events.published if isinstance(event, kind)]


def _song(id_: int, title: str) -> Song:
    return Song(
        id=SongId(id_),
        title=title,
        artist_id=ArtistId(1),
        album_id=AlbumId(1),
        file_path=Path(f"/opt/music/Bad_Company/Bad_Company/{title}"),
        file_format=AudioFormat.MP3 if title.endswith("mp3") else AudioFormat.M4A,
        duration=timedelta(seconds=210),
    )


@pytest.fixture
def appliance() -> Appliance:
    return Appliance()


def _request_the_library(appliance: Appliance) -> None:
    for song_id, title in REQUESTS:
        appliance.request(song_id, title, detail=AUDIO_OUTPUT_FAILURE)


# -- the machine, not the song -------------------------------------------


def test_an_engine_with_no_audio_output_is_not_reported_as_a_bad_file(
    appliance: Appliance, caplog: pytest.LogCaptureFixture
) -> None:
    """The log line is where #48 was read, and it was reading it wrong.

    Three tracks refused is not three files: it is one machine, said once. The fields carry that,
    which is why the assertion is about `fault_kind` and the absence of `file` rather than about a
    sentence an operator has to recognise.
    """

    with caplog.at_level(logging.DEBUG, logger="encore.playback"):
        _request_the_library(appliance)

    faulted = [record for record in caplog.records if getattr(record, "fault_kind", None)]
    assert [record.__dict__["fault_kind"] for record in faulted] == ["audio_output"] * len(REQUESTS)
    assert not [r for r in caplog.records if "file" in r.__dict__], (
        "the old line blamed whichever guest asked last"
    )
    errors = [r for r in faulted if r.levelno == logging.ERROR]
    assert len(errors) == 1, "one fault, reported once, not three times in a row"
    assert errors[0].__dict__["refused_tracks"] == 1
    assert faulted[-1].__dict__["refused_tracks"] == len(REQUESTS), (
        "the running count is what turns a fourth refusal into a state of the machine"
    )


def test_the_fault_reaches_the_appliances_health_not_just_the_players(
    appliance: Appliance,
) -> None:
    """The half that cost the hour: `/health` was true, and useless.

    Aggregated, because a component reporting degraded that the banner never showed would be the
    same lie in a different place (SAPRS 11.6, 11.7 step 8).
    """

    _request_the_library(appliance)

    assert appliance.playback.fault is not None
    assert appliance.playback.fault.kind is FaultKind.AUDIO_OUTPUT
    snapshot = appliance.health.check()
    assert snapshot.status is not HealthStatus.HEALTHY, snapshot.detail
    assert snapshot.components[0].status.value == "degraded"
    assert AUDIO_OUTPUT_FAILURE in snapshot.detail, "mpv's words are the searchable part"
    assert "audio.device" in snapshot.detail, "and the setting that is actually wrong"


def test_a_corrupt_file_still_costs_the_appliance_nothing(appliance: Appliance) -> None:
    """The other direction, and the reason the rule had to be narrow.

    A library this size has unreadable files in it. Were a refusal of any kind a component fault,
    playback would sit degraded for the rest of the night and the banner would stop meaning
    anything — including on the night of the hour #48 describes.
    """

    appliance.request(42, "sting.m4a", detail="loading failed")

    assert appliance.playback.fault is not None
    assert appliance.playback.fault.kind is FaultKind.SONG_FILE
    assert "loading failed" in appliance.playback.fault.summary
    assert appliance.health.check().status is HealthStatus.HEALTHY


# -- what did not change -------------------------------------------------


def test_every_refused_track_is_still_reported_once_and_the_party_moves_on(
    appliance: Appliance,
) -> None:
    """SAPRS 11.9 and 8.7, unchanged by the classification.

    The event is the same for a broken file and a broken sound card on purpose: the queue's job is
    to settle the item and take the next one, and #48's fix is what happens *around* that, not
    instead of it. A queue that stalled on an output fault would turn a silent appliance into a
    stuck one.
    """

    _request_the_library(appliance)

    assert appliance.events_of(SongStarted) == [], "nothing played, so nothing was announced"
    finished = [
        event for event in appliance.events_of(SongFinished) if isinstance(event, SongFinished)
    ]
    assert [(int(e.song_id), e.reason) for e in finished] == [
        (song_id, FinishedReason.FAILED) for song_id, _ in REQUESTS
    ]
    assert all(e.completion == 0.0 for e in finished)
    assert appliance.playback.state is PlaybackState.IDLE


def test_the_first_track_that_plays_clears_the_appliance(
    appliance: Appliance,
) -> None:
    """The remedy is a device name, and the appliance has to notice when it is given one.

    Nothing here can restart the fault away — an mpv that comes back reads the same `audio:` and
    gets the same answer — so the only evidence that ends it is sound.
    """

    _request_the_library(appliance)
    assert appliance.health.check().status is not HealthStatus.HEALTHY

    appliance.mpv.properties.update(
        {"filename": "/opt/music/next.mp3", "idle-active": False, "eof-reached": False}
    )
    assert appliance.playback.play(_song(500, "Ready.mp3"), queue_item_id=QueueItemId(500)) is True
    appliance.playback.tick()

    assert appliance.playback.fault is None
    assert appliance.health.check().status is HealthStatus.HEALTHY
    assert appliance.events_of(SongStarted), "and the track that worked is reported as working"


def test_the_fault_is_a_fact_about_the_engine_not_about_a_process(
    appliance: Appliance,
) -> None:
    """Recovery must not be allowed to look like a repair.

    This is the same trap as the queue's recovery handler falling into at #23: the reassuring event
    and the underlying condition are different objects. Here the risk is the reverse — a supervisor
    that announced `PlaybackRecovered` and took that as "the sound card is fine again".
    """

    _request_the_library(appliance)
    appliance.mpv.crash()
    appliance.supervisor.tick()

    assert appliance.supervisor.restart_count >= 1, "the death was recovered"
    assert appliance.playback.fault is not None
    assert appliance.health.check().components[0].status.value in {"degraded", "unavailable"}, (
        "a restart changed the process, not the device"
    )
