"""The contexts the administrative screens render (SAPRS 1.3, 8.8, 10.3, AIG 14).

The same rule `views.py` states for the guest's pages, applied to the operator's: build the
context, on the appliance thread, in as few reads as the screen can be honest with — and
present nothing. Templates reach no service, and this module decides nothing that a service
has not already decided; what is here is *which* questions the administrator's screen asks.

The split from `views.py` is by audience rather than by mechanism. A guest's page asks "what
is playing and what is waiting"; an operator's asks those plus "how is the box, how often is
it used, what did it log, and what is it configured to do". Two modules keep each of those
lists readable as a list, which a single file with eleven context builders would not.

Three decisions that belong here rather than in a route or a template:

* **Health is freshly checked on an administrative page.** `panels/alerts.html` on a guest's
  screen reads the last snapshot because a guest must not pay for a probe; an operator who
  opened `/admin/health` *is* asking the question, and SAPRS 1.5's "clear health information"
  is about the screen that says it is fine when it is not. The `check()` also performs the
  publish-if-moved that the tick performs, which is the same rule as `/fragments/alerts`.
* **The configuration summary is rendered from the validated model, not from the file.**
  `config.yaml` may contain a key this build ignores or a default this build changed; the
  model is what the appliance is actually running (SAPRS 12.5's startup validation is what
  makes the two agree, and the summary shows the *result*). Values whose names read as
  secrets go through `utilities.redaction`, which is the same list the log formatter uses.
* **One report, not six counters.** `StatisticsService.report()` is a single transaction; a
  page that asked for each number separately would print a total smaller than the "today"
  beside it whenever a song ended mid-render, which is the bug a statistics screen is most
  likely to have and the least likely to notice.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from encore.api import views
from encore.api.deps import Jukebox
from encore.api.rows import rows_for_entries, rows_for_songs
from encore.config.models import EncoreConfig
from encore.services.library_service import LibrarySummary
from encore.services.logging_service import LoggedEvent
from encore.services.statistics_service import StatisticsReport
from encore.utilities.redaction import redact

__all__ = [
    "LOG_LEVELS",
    "LOG_LIMIT",
    "configuration_context",
    "dashboard_context",
    "health_context",
    "library_context",
    "logs_context",
    "queue_context",
    "statistics_context",
]

#: The levels the log view can filter to, in the order a worried operator reads them.
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

#: Lines one page shows. The journal has all of them; this is the last screenful of the
#: appliance's own memory (see `LogTail`), and a page that printed four hundred lines would
#: be a page nobody scrolls.
LOG_LIMIT = 200


def dashboard_context(jukebox: Jukebox) -> dict[str, Any]:
    """What is playing, what is waiting, and whether the box is well (SAPRS 1.5, 9.8).

    The guest's view plus a fresh health aggregate, because the dashboard is the screen an
    operator opens when something sounds wrong — and one fact they cannot get from the guest
    pages: whether the appliance has had to recover.
    """

    view = views.player(jukebox)
    return {
        **view.context,
        "view": view,
        "health": jukebox.health.check(),
        "sessions": len(jukebox.admin.sessions()),
    }


def queue_context(jukebox: Jukebox) -> dict[str, Any]:
    """Every item, with the queue's own status beside each one (SAPRS 8.8, 9.9).

    The uncapped list, like `/queue`: the subject of this screen *is* the list, and an
    operator removing a stuck request needs to see the whole of it rather than the twenty
    rows a phone renders. `status` comes from `QueueEntry.item`, so a row says what the
    queue believes rather than what the player appears to be doing.
    """

    current = jukebox.queue.now_playing()
    waiting = jukebox.queue.up_next(limit=None)
    entries = ([current] if current is not None else []) + list(waiting)
    return {
        "rows": rows_for_entries(entries, jukebox.library),
        "length": jukebox.queue.length,
        "max_items": jukebox.queue.max_items,
        "wait": jukebox.queue.wait_time(),
    }


def health_context(jukebox: Jukebox) -> dict[str, Any]:
    """The aggregate and every component's own words (SAPRS 11.7's step 8, 11.9)."""

    health = jukebox.health.check()
    return {"health": health, "components": health.components}


def statistics_context(jukebox: Jukebox) -> dict[str, Any]:
    """The counters, the play log's shape, and the leaderboard with its labels.

    The leaderboard is the one place in the interface that resolves a `SongId` to a title for
    a *statistic*, which is why it costs one batched read rather than one per row
    (`encore/api/rows.py`'s rule, applied to the admin screen as everywhere else).
    """

    report: StatisticsReport = jukebox.stats.report()
    ids = [song for song, _ in report.leaderboard]
    labelled = {
        int(row.song.id): row
        for row in rows_for_songs(jukebox.library.songs_by_ids(ids), jukebox.library)
        if row.song
    }
    return {
        "report": report,
        # Paired here rather than joined in the template, and in the report's order rather
        # than the library's: a leaderboard whose rows were sorted by the database would be a
        # leaderboard that changed rank between two refreshes of one page.
        "leaderboard": [(labelled.get(int(song)), plays) for song, plays in report.leaderboard],
        "library": jukebox.library.counts(),
    }


def logs_context(
    jukebox: Jukebox, *, level: str | None = None, limit: int = LOG_LIMIT
) -> dict[str, Any]:
    """The tail of the appliance's own records, newest first (AIG 14's "Logs").

    No query text and no per-client filter, deliberately: what is being filtered is the
    appliance's log, and a screen that could ask "what did this phone do" would be a screen
    that had started to keep track of phones (ADR-007).
    """

    lines = jukebox.logs.lines(limit=limit, minimum=level)
    return {
        "lines": lines,
        "level": level,
        "levels": LOG_LEVELS,
        "kept": len(jukebox.logs),
        "counts": _levels(lines),
    }


def configuration_context(jukebox: Jukebox) -> dict[str, Any]:
    """The running configuration, as one table per section (SAPRS 12.3's read-only summary).

    There is no editing here and there is no form that would post to one: the YAML file is
    installation-time, and an administrative interface that wrote it back would be an
    application-managed database with a `.yaml` extension, which SAPRS 12.7 goes further and
    forbids by name. The page says that in one sentence, because an operator looking for the
    volume slider deserves to be told why there isn't one rather than to hunt for it.
    """

    config: EncoreConfig = jukebox.config
    return {
        "sections": _sections(config),
        "admin": {
            "session_minutes": config.admin.session_minutes,
            "credential": jukebox.admin.is_configured(),
            "last_login": jukebox.admin.last_login(),
        },
    }


def library_context(jukebox: Jukebox) -> dict[str, Any]:
    """Library information, and the artwork cache's own reality beside it (AIG 14)."""

    summary: LibrarySummary = jukebox.library.summary()
    return {
        "summary": summary,
        "search_available": jukebox.library.search_available,
        "artwork_dir": str(jukebox.config.paths.artwork_dir),
        "artwork_present": Path(jukebox.config.paths.artwork_dir).is_dir(),
    }


# -- pieces of the above that are arithmetic rather than policy ----------------


def _sections(config: EncoreConfig) -> list[tuple[str, list[tuple[str, object]]]]:
    """One row per setting, grouped by section, with sensitive names hidden.

    Two levels rather than a recursive walk: `EncoreConfig` is a model of sections, and the
    shape of the page should be the shape of the model, so a new section appears in the
    summary the day it can be configured at all.
    """

    rows: list[tuple[str, list[tuple[str, object]]]] = []
    for section, fields in config.model_dump().items():
        values: Mapping[str, Any] = (
            fields if isinstance(fields, Mapping) else {str(section): fields}
        )
        rows.append(
            (
                str(section),
                [
                    (str(key), _show(redact(str(key), value)))
                    for key, value in sorted(values.items())
                ],
            )
        )
    return rows


def _show(value: object) -> object:
    """A configuration value as one cell of a table."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(str(_show(item)) for item in value)
    if isinstance(value, Mapping):
        return ", ".join(f"{key}={_show(item)}" for key, item in sorted(value.items()))
    if value is None:
        return "—"
    return value


def _levels(lines: tuple[LoggedEvent, ...]) -> Mapping[str, int]:
    """How many of each level are in view, so a filter has something to be against."""

    counts = dict.fromkeys(LOG_LEVELS, 0)
    for line in lines:
        if line.level in counts:
            counts[line.level] += 1
    return counts
