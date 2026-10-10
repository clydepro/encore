"""How a fact reads to a guest (SAPRS 1.5, 9.8, 11.9).

One function, because the alternative is a sentence in a template and the same sentence
reworded in a second template six months later. The strip in `panels/alerts.html` asks for
a line of text; this decides what the line is.

Three rules the wording follows:

* **Say what happened, not what broke.** "That song could not be started" is a fact a guest
  can act on by asking for another one; "playback error" is a log line on a screen.
* **Never promise a fix that is not happening.** A recovery says the music resumed because
  `PlaybackRecovered` is published *after* it did, and a health change says nothing is
  implied at all.
* **No blame.** SAPRS 8.4's anonymity means there is no name to print beside a request, and
  a strip that said "someone skipped your song" would be describing a person rather than a
  jukebox.

An event with no sentence here renders as nothing rather than as a class name: a guest
should not be told the appliance's vocabulary.
"""

from __future__ import annotations

from encore.events import Event

__all__ = ["SENTENCES", "describe"]

#: Fact type name to line of text. Written by name for the reason `fragments.py` gives: a
#: reviewer should be able to read the list of things the appliance will tell a guest and
#: check it against SAPRS 9.10, and an import of eight event classes would be a second
#: place to say the same thing.
SENTENCES = {
    "PlaybackRecovered": "Playback had a problem and has resumed.",
    "LibraryReloaded": "The library has been rebuilt — new songs are available.",
}


#: `HealthChanged` is the interesting case: the sentence belongs to the *change* rather
#: than to the event type, and the fact already carries the component that moved and what it
#: said (SAPRS 1.5). So its line is assembled from the payload, in the same shape
#: `HealthSnapshot.detail` uses, so a banner redrawn by a fact and a banner rendered at page
#: load cannot read differently.
def describe(event: Event | None) -> str:
    """One line for the strip, or an empty string for a fact with nothing to say."""

    if event is None:
        return ""
    sentence = SENTENCES.get(type(event).__name__)
    if sentence is not None:
        return sentence
    if type(event).__name__ == "HealthChanged":
        component = str(getattr(event, "component", ""))
        detail = str(getattr(event, "detail", ""))
        if component and detail:
            return f"{component}: {detail}"
        return component or detail or "This jukebox has a problem."
    return ""
