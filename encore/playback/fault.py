"""Why the engine refused, as one object the log and health can agree on (SAPRS 7.2, 11.7).

Two refusals arrive at the engine identically — `loadfile` accepted, then an `end-file` with
`reason: "error"`, and a player idle with no filename and no position — and differ only in
mpv's own words for the reason. Telling them apart matters because they are answered in
different places and at different scales:

* A refused **file** is one song. The queue walks past it, the guest is told, and the
  appliance is fine. SAPRS 11.9's "reported, not swallowed" is satisfied by one WARNING and
  a `SongFinished(FAILED)`.
* A refused **audio output** is every song, in both formats, forever, until someone edits
  `audio:`. It is not a fact about a track, and filing it as one is what makes an appliance
  that cannot make sound report itself healthy: mpv is up, the IPC socket answers, the
  supervisor has nothing to restart. This is the failure the unit file's own comment calls
  out — "an appliance in a sandbox with no sound card reports healthy and plays nothing" —
  and it does not require a sandbox to happen. A Raspberry Pi 4 with nothing plugged into
  its HDMI port refuses `alsa:default`, because `default` is card 0 and card 0 is that port.

So the distinction gets one type, constructed where the refusal is read, and read by the two
places that have to say something about it: `PlaybackService` (which logs) and
`PlaybackSupervisor` (which reports health). Before it, both said nothing useful: one
blamed the last file someone happened to request, and the other said "mpv running as pid".

Nothing in this module talks to mpv; it is a record of a conclusion someone else reached.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from encore.domain import SongId
from encore.utilities.clock import ensure_aware

__all__ = ["FaultKind", "PlaybackFault"]


class FaultKind(StrEnum):
    """Which thing the engine said no to.

    The names are the answer to "whose fault is it", which is the only question an operator
    reading a health page can act on.
    """

    #: This track. The next one may well play.
    SONG_FILE = "song_file"

    #: The sound device. The next track will not play, nor will the one after that.
    AUDIO_OUTPUT = "audio_output"


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaybackFault:
    """One engine refusal, classified, with how long it has been going on.

    Attributes:
        kind: Whose fault it is, per `FaultKind`.
        detail: mpv's own words (`file_error`), because the engine's sentence is the one
            that matches what a search for the fix will find.
        song_id: The track that proved it — the last one requested, not the first.
        occurred_at: When this refusal happened, for a health page that must be able to say
            whether it is current or stale.
        repeats: How many tracks have been refused in a row for this same reason. One is an
            event; forty is the state of the machine, and the number is what tells those
            two stories apart without reading forty log lines.
    """

    kind: FaultKind
    detail: str
    song_id: SongId
    occurred_at: datetime
    repeats: int = 1

    def __post_init__(self) -> None:
        ensure_aware(self.occurred_at, field_name="occurred_at")

    @property
    def blocks_every_track(self) -> bool:
        """Whether the next request will fail the same way.

        Named for the decision rather than for the kind, because that decision is the
        reason the distinction exists: health may go degraded on this, and nothing else
        changes. A broken file in a 15,000-song library is not a broken appliance.
        """

        return self.kind is FaultKind.AUDIO_OUTPUT

    @property
    def summary(self) -> str:
        """One line, for a health report and for the log's `detail` field.

        The fix is named in the sentence because this string is what someone reads when
        there is no manual open: mpv has said it cannot make sound, and the two settings
        that decide which sound card that means are `audio.output` and `audio.device`
        (SAPRS 7.7).
        """

        if self.kind is not FaultKind.AUDIO_OUTPUT:
            return f"mpv refused a track: {self.detail}"
        tracks = "1 track" if self.repeats == 1 else f"{self.repeats} tracks"
        return (
            f"mpv cannot open the audio output: {self.detail}; "
            f"{tracks} refused in a row — check audio.output and audio.device (SAPRS 7.7)"
        )

    def repeated(self, *, detail: str, song_id: SongId, occurred_at: datetime) -> PlaybackFault:
        """This fault again, one track later, with what mpv said this time.

        `repeats` grows rather than a new record being built, so a fault that has been true
        for an hour is distinguishable from one that started with this request. The kind is
        carried over and the words are not: mpv may phrase the same refusal differently
        across two files, and the sentence shown in health should be the one it said most
        recently.
        """

        return replace(
            self,
            detail=detail,
            song_id=song_id,
            occurred_at=occurred_at,
            repeats=self.repeats + 1,
        )
