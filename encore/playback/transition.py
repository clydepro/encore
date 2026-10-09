"""How one track becomes the next (SAPRS 7.8, 12.2, AIG 10).

Gapless and crossfade are the two behaviours in Encore that cannot be described as "do
the thing": each is a timing policy plus a set of engine options, and both belong to
configuration rather than code (SAPRS 7.7). So they are one object here, and the rest of
the playback layer asks it two questions — what must mpv be launched with
(`mpv_options`), and what should the volume be right now (`volume_for`).

**What v1's crossfade is, exactly.** Encore runs one mpv per appliance, so it cannot mix
two streams into one overlap: that needs a second deck, a second socket and a second
crash surface. What it can do, and what `audio.crossfade_seconds > 0` selects, is a
*linear fade*: down to silence across the last `crossfade` seconds of a track and up from
silence across the first `crossfade` seconds of the next, with the transition still
happening at the end of file so nothing is cut short.

That is a fade, not a mix, and the distinction is written here rather than in a README
because it is the kind of promise a listener notices. SAPRS 16.4 books "crossfade
improvements" for 1.2, and a second deck would be an ADR of its own — one more process,
one more socket, one more thing for the Supervisor to revive. What is not deferred is the configuration surface, the policy's place in
the playback layer, and the command-sequence tests — which is what SAPRS 7.8 asks for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Self

from encore.config.models import AudioConfig

__all__ = ["MINIMUM_CROSSFADE", "TransitionPolicy"]

#: The shortest overlap worth running a ramp for. Below this, volume commands arrive
#: closer together than one progress tick and the "fade" is a hard cut with extra steps.
MINIMUM_CROSSFADE: Final = timedelta(milliseconds=250)


@dataclass(frozen=True, slots=True, kw_only=True)
class TransitionPolicy:
    """The gapless and crossfade settings of one appliance.

    Attributes:
        gapless: Ask mpv for gapless audio output. Off means mpv may close and reopen the
            device between tracks, which some USB DACs need and which no amount of
            software crossfade fixes.
        crossfade: How long each half of the fade lasts. Zero selects a plain transition.
        volume: The configured output level, which is where a ramp starts and ends.
    """

    gapless: bool = True
    crossfade: timedelta = timedelta(0)
    volume: float = 100.0

    @classmethod
    def from_config(cls, audio: AudioConfig) -> Self:
        """Read `audio:`. The only place configuration becomes playback behaviour."""

        return cls(
            gapless=audio.gapless,
            crossfade=timedelta(seconds=max(0.0, audio.crossfade_seconds)),
            volume=float(audio.volume),
        )

    @property
    def fading(self) -> bool:
        """Whether a fade is configured at all."""

        return self.crossfade >= MINIMUM_CROSSFADE

    def mpv_options(self) -> tuple[str, ...]:
        """The launch options this policy needs. One, because mpv has one gapless option."""

        return (f"--gapless-audio={'yes' if self.gapless else 'no'}",)

    def volume_for(self, position: timedelta, duration: timedelta) -> float | None:
        """The level to hold at this moment, or None when no ramp is in progress.

        None is the common answer and the important one: a volume command on every tick
        is 60 IPC round trips a minute on a Pi 4 that has better things to do, and mpv
        does not need to be told the number has not changed.
        """

        if not self.fading or duration <= timedelta(0):
            return None
        remaining = duration - position
        if timedelta(0) <= remaining <= self.crossfade:
            return self.volume * (remaining / self.crossfade)
        if timedelta(0) <= position < self.crossfade:
            return self.volume * (position / self.crossfade)
        return None

    def final_volume(self) -> float:
        """The level to leave the engine at when playback stops or a fade is abandoned.

        Asked by the service rather than remembered: a track skipped three seconds into a
        fade-in would otherwise hand the next one a volume of 20 and no way to notice.
        """

        return self.volume
