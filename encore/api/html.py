"""Rendering, and the two caching rules that go with it (SAPRS 9.3, 10.10).

One Jinja environment, held by the application rather than by a module (AEP 9), and a
`Response` factory that remembers the headers a jukebox's endpoints need:

* **A fragment is never cached.** Now Playing and Up Next are true for one second.
  SAPRS 10.10 asks for exactly this asymmetry — artwork and library assets cacheable,
  mutable state not — and a browser that caches `/fragments/now-playing` shows a song
  that ended a minute ago.
* **Artwork caches forever, until it cannot.** A file in the artwork cache is
  content-addressed by the Builder and immutable until the next build (ADR-006), so
  `immutable` is the honest directive; the `ETag` from `ArtworkFile.etag` is what makes
  a reload cheap when a build *has* happened.

`Renderer` rather than `Jinja2Templates` for a reason as much as a preference:
Starlette's wrapper wants a `request` in every context, and a server-rendered page that
cannot be produced without one is a page that cannot be unit-tested. Autoescape is on by
extension, because a guest's search text and a filename from `library.db` both reach a
screen, and an escaping bug here is an XSS on every phone at one party.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

__all__ = [
    "FRAGMENT_HEADERS",
    "NO_COVER",
    "NO_COVER_MEDIA_TYPE",
    "PLACEHOLDER_HEADERS",
    "STATIC_DIR",
    "TEMPLATES_DIR",
    "Renderer",
    "artwork_headers",
    "build_renderer",
]

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PACKAGE_ROOT / "templates"
STATIC_DIR = PACKAGE_ROOT / "static"

#: What every mutable fragment is sent with (SAPRS 10.10).
FRAGMENT_HEADERS: Mapping[str, str] = {
    "Cache-Control": "no-store, max-age=0",
    "X-Accel-Buffering": "no",  # a proxy in front of the appliance must not buffer a swap
}


class Renderer:
    """The template environment, plus the filters a template cannot do without.

    Args:
        directory: Template root. Defaults to `encore/templates/`; a test may point it
            elsewhere, which is cheaper than routing every assertion through the
            production templates.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._environment = Environment(
            loader=FileSystemLoader(str(directory or TEMPLATES_DIR)),
            autoescape=select_autoescape(("html", "xml", "jinja", "j2")),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._environment.filters["duration"] = _clock
        self._environment.filters["clock"] = _clock
        self._environment.filters["artwork_url"] = _artwork_url
        self._environment.filters["json"] = _json
        self._environment.filters["alert_text"] = _alert_text

    def render(self, name: str, context: Mapping[str, Any]) -> str:
        template = self._environment.get_template(name)
        return template.render(dict(context))

    def response(
        self,
        name: str,
        context: Mapping[str, Any],
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """One rendered template as an HTML response, with the mutable-state headers on.

        `headers` overrides rather than extends for `Cache-Control` specifically: a page
        shell is cacheable for a few seconds where its fragments are not, and the
        difference is one argument rather than one more subclass.
        """

        return Response(
            content=self.render(name, context),
            media_type="text/html; charset=utf-8",
            status_code=status_code,
            headers=dict(headers or FRAGMENT_HEADERS),
        )


def build_renderer() -> Renderer:
    """The environment `create_app` holds. A function, not a module global (AEP 9)."""

    return Renderer()


#: The picture for a thing with no picture. An `<img>` must resolve to something, and a 404
#: in a track listing is a broken icon and a console full of noise on a page that is fine.
NO_COVER = STATIC_DIR / "img" / "no-cover.svg"
NO_COVER_MEDIA_TYPE = "image/svg+xml"

#: Not cacheable, unlike a real cover: the placeholder stands in for "nothing is depicted
#: *yet*", and a library rebuild can change the answer for the same URL.
PLACEHOLDER_HEADERS: Mapping[str, str] = {"Cache-Control": "no-store, max-age=0"}


def artwork_headers(etag: str | None = None) -> dict[str, str]:
    """The caching a content-addressed file deserves (SAPRS 10.10)."""

    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    if etag is not None:
        headers["ETag"] = etag
    return headers


def _clock(value: object) -> str:
    """A `timedelta`, a number of seconds, or None, as clock text.

    `None` renders as an em-dash rather than `0:00`, because a library row with no
    decoded length is a different statement from a two-second track and a guest can
    tell the difference.
    """

    if value is None:
        return "—"
    if isinstance(value, timedelta):
        total = int(value.total_seconds())
    elif isinstance(value, (int, float)):
        total = int(value)
    else:
        return "—"
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _artwork_url(value: object, kind: str = "album") -> str:
    """The appliance's own path to a picture, or the placeholder.

    `kind` matches `ArtworkKind` (`album`, `artist`, `song`) because the URL is
    addressed by what is depicted; the repository already falls back from a song to its
    album's cover, which is why a song row can point at one path and be right (SAPRS 6.7).
    """

    if value is None:
        return "/static/img/no-cover.svg"
    return f"/artwork/{kind}/{value}"


def _alert_text(event: object) -> str:
    """A fact, as the sentence a guest reads.

    A local import rather than a top-level one: `encore/api/fragments.py` imports this
    module to render, and `alerts.py` is only ever reached *from* a render, so the edge has
    to point this way or the package cannot be imported at all.
    """

    from encore.api.alerts import describe  # noqa: PLC0415 - see the cycle above

    text: str = describe(event)  # type: ignore[arg-type]
    return text


def _json(value: object) -> str:
    """Encode a value for an HTML attribute.

    `|tojson` would do this too. Naming it says that this particular attribute is
    machine-read, which `tojson` does not.
    """

    return json.dumps(value, separators=(",", ":"), default=str)
