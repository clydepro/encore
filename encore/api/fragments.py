"""Which part of the screen each fact makes stale (SAPRS 9.3, 9.8-9.10).

One table, because the alternative is every fact being rendered for every open
connection by a handler that guesses. The bus delivers one event per publication; this
module says which regions it invalidates, and the HTML for those regions is rendered
**once** and reused for every client — the difference between a party of forty and forty
renders of the same dozen rows.

The regions are the two halves of the guest's persistent shell:

* **`player`** — Now Playing, progress, Up Next: SAPRS 9.8 and 9.9.
* **`alerts`** — the strip for facts a guest should hear rather than infer: a recovery, a
  health change, a rebuilt library (SAPRS 1.5's "clear status"), kept out of the music
  panel so a degraded mpv does not look like a changed song.

A fact that changes neither renders nothing. `SongQueued` is the example: the guest who
asked gets their answer in the POST's own response, and everyone else's Up Next arrives
with the `QueueAdvanced` or `SongStarted` that follows it.

Frames a region is sent under are named `swap:<region>` rather than after the fact that
caused them, deliberately: a browser that subscribes to `swap:player` need not know that
`SongFinished` and `QueueAdvanced` are different events, and teaching it that they are
would be teaching it the domain, which is what `/api/v1` is for.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from encore.api import views
from encore.api.deps import Jukebox
from encore.api.html import FRAGMENT_HEADERS, Renderer
from encore.events import EVENT_VOCABULARY, Event
from encore.events.bus import Subscription

__all__ = [
    "EVENT_REGIONS",
    "REGION_ALERTS",
    "REGION_PLAYER",
    "push",
    "subscribe",
    "swap_frame",
    "swap_headers",
]

REGION_PLAYER = "player"
REGION_ALERTS = "alerts"

#: Written by name rather than by class so the table a reviewer checks against SAPRS
#: 9.10's list is one block of text, and importing eight event types to state it would be
#: a second place to say the same thing. A name listed here that stops existing is caught
#: by `test_every_named_region_is_a_fact`, not by a browser.
_REGIONS_BY_NAME: Mapping[str, tuple[str, ...]] = {
    "SongStarted": (REGION_PLAYER,),
    "SongFinished": (REGION_PLAYER,),
    "QueueAdvanced": (REGION_PLAYER,),
    "PlaybackRecovered": (REGION_ALERTS,),
    "HealthChanged": (REGION_ALERTS,),
    "LibraryReloaded": (REGION_PLAYER, REGION_ALERTS),
}

#: Derived from `EVENT_VOCABULARY`, so a proposed ninth fact shows up here with an empty
#: region list — seen, and deliberately unwired — rather than being absent (ADR-004).
EVENT_REGIONS: Mapping[type[Event], tuple[str, ...]] = {
    fact: _REGIONS_BY_NAME.get(fact.__name__, ()) for fact in EVENT_VOCABULARY
}

#: The template each region is.
REGION_TEMPLATES: Mapping[str, str] = {
    REGION_PLAYER: "panels/player.html",
    REGION_ALERTS: "panels/alerts.html",
}


def swap_frame(region: str) -> str:
    """The SSE event name a region's frames carry."""

    return f"swap:{region}"


def push(jukebox: Jukebox, renderer: Renderer, event: Event) -> None:
    """Render the regions one fact invalidates and send them once to everyone.

    Called from a bus handler, so on the appliance thread (ADR-012), which is where the
    reads a render performs belong. A region that cannot render is not this fact's
    failure: it is logged, and the facts still reach the machine clients, because SAPRS
    11.9 says a presentation fault must not stop a party.
    """

    regions = EVENT_REGIONS.get(type(event), ())
    if not regions:
        return
    # One read of the appliance for every region this fact invalidates, rather than one
    # per region: two regions from one fact is the common case, and two reads of a state
    # that can change between them is how a panel shows a song that already ended.
    snapshot = views.player(jukebox) if REGION_PLAYER in regions else None
    for region in regions:
        context: dict[str, Any] = (
            dict(snapshot.context) if snapshot is not None else _alerts_context(jukebox, event)
        )
        if region == REGION_ALERTS:
            context.update(_alerts_context(jukebox, event))
        try:
            html = renderer.render(REGION_TEMPLATES[region], context)
        except Exception:
            jukebox.logger.exception("fragment render failed", extra={"region": region})
            continue
        jukebox.live.push_html(swap_frame(region), html)


def subscribe(jukebox: Jukebox, renderer: Renderer) -> list[Subscription]:
    """Watch the bus for the facts that redraw the shell.

    The subscriptions are handed back rather than kept here, so shutdown can withdraw
    them: a handler left registered outlives the app that made it and would raise into a
    process that has stopped listening.
    """

    def handle(event: Event) -> None:
        push(jukebox, renderer, event)

    return [
        jukebox.events.subscribe(event_type, handle, name=f"fragments.{event_type.__name__}")
        for event_type in EVENT_REGIONS
        if EVENT_REGIONS[event_type]
    ]


def swap_headers(regions: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Headers for a response that redraws a region HTMX did not ask about.

    One action can redraw two panels, and HTMX's out-of-band swap does it from a single
    response — the difference between a song tap costing one round trip and three, on a
    phone three rooms from the access point where the round trip is the whole cost.
    """

    headers: dict[str, str] = dict(FRAGMENT_HEADERS)
    if regions:
        headers["HX-Retarget"] = "body"
        headers["HX-Reswap"] = "outerHTML"
    return headers


def _alerts_context(jukebox: Jukebox, event: Event) -> dict[str, Any]:
    return {
        "health": jukebox.health.snapshot,
        "fact": event,
        "domain": jukebox.config.server.domain,
    }
