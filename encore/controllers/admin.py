"""The administrator's routes: one surface, eleven questions (SAPRS 8.8, 10.8, AIG 14).

Everything a guest can do is in `queue.py`; everything an operator can do is here, and the
difference between the two files is the difference between a jukebox and an appliance. The
list is AIG 14's: sign in, see what is playing, control playback, manage the queue, and read
the health, the statistics, the logs, the configuration and the library.

Four things about the shape of this module are decisions rather than style, so they are
stated where they happen.

**Sign-in is a router, not a decorator.** This module exports two: `router` holds
`/admin/login` and nothing else, and `secure` holds every other route with `Depends(depend_admin)`
applied at the router, so a route added to `secure` cannot be public by forgetting a decorator.
That is the property an interface like this needs — the guest surface fails in one direction
(a page that does not load), and this one fails open into somebody else's player. The cost is
that a route's signature no longer announces its requirement, and
`tests/integration/test_admin_interface.py` pays for it by walking the live route table and
asserting that every `/admin` path except the sign-in pair is behind the dependency.

**A screen is one template rendered two ways.** `browse.py` and `panels.py` already share a
panel template; `/admin` keeps that convention inside itself, so `GET /admin/logs` answers an
`HX-Request` with `admin/panels/logs.html` and a browser with the shell around it — the same
context builder either way, which is the only reason the two cannot disagree. It matters here
more than anywhere, because the administrative pages are the refreshable ones: a health screen
that needed a full navigation to update would be refreshed by hand, which is polling with a
finger on it, and SAPRS 12.4's one-second budget would then be measured against a human rather
than against a stream. The refresh is `encore-live.js`'s `encore:change` event, and an action
answers by re-rendering the region that asked for it (`?panel=`), so there is exactly one
markup for exactly one screen.

**Control actions go through the queue, never to the player.** The five transport commands and
the two queue mutations are `QueueService`'s administrative methods, which exist for this route
and publish the facts that make every guest's screen move. A handler that called
`PlaybackService` instead would move the speakers without moving the interface — ADR-011's
forbidden second path, in its most audible form: the song changes and forty phones keep showing
the old one.

**A failure answers in the language it was asked in.** HTMX gets the region it pointed at; a
browser without JavaScript gets a 303 back to the dashboard, where the state it just changed is
visible in the same place it would have appeared; a tool gets the JSON envelope SAPRS 10.7
describes. That is `queue.py`'s split reused rather than reinvented, and it is why an operator
on a phone with a dead service worker can still run the appliance.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import partial
from typing import Annotated, Any, Final

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import RedirectResponse

from encore.api import admin_views
from encore.api.auth import clear_session, depend_admin, session_cookie, session_token
from encore.api.deps import Jukebox, jukebox_of, read, renderer_of
from encore.api.errors import RATE_LIMITED, UNAUTHORIZED, respond
from encore.api.html import FRAGMENT_HEADERS, Renderer
from encore.domain import QueueItemId
from encore.services.admin_service import MIN_PASSWORD_BYTES
from encore.services.errors import AdminError, AuthenticationError, RateLimitError

__all__ = ["router", "secure"]

#: Where a browser that signed in, or pressed a button without JavaScript, is sent back to.
_RETURN = "/admin/dashboard"

#: The five transport commands and the one `QueueService` method each of them is. A table
#: rather than five handlers because the handler is identical five times and the only
#: difference is the verb — and because a test can iterate it and assert all five work.
COMMANDS: dict[str, str] = {
    "pause": "pause",
    "resume": "resume",
    "skip": "skip",
    "stop": "stop",
    "start": "play_next",
}

#: Each screen's own assembly of its context, keyed by the name the URL and the template use.
#: Both the page route and the action that refreshes it look the builder up here, which is what
#: makes "an action answers with the region that asked" a one-line claim rather than a second
#: implementation of every screen. `logs` is absent because it takes arguments.
PANELS: dict[str, Callable[[Jukebox], dict[str, Any]]] = {
    "dashboard": admin_views.dashboard_context,
    "queue": admin_views.queue_context,
    "health": admin_views.health_context,
    "statistics": admin_views.statistics_context,
    "configuration": admin_views.configuration_context,
    "library": admin_views.library_context,
}

router = APIRouter(prefix="/admin", tags=["admin"])

#: Everything on this router is behind the session. See the docstring's first point.
secure = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(depend_admin)])

Juke = Annotated[Jukebox, Depends(jukebox_of)]
Render = Annotated[Renderer, Depends(renderer_of)]
Htmx = Annotated[str | None, Header(alias="HX-Request")]
Panel = Annotated[str, Query(pattern="^(dashboard|queue|library)$")]

#: A shell is as mutable as the fragment inside it.
_SHELL = {"Cache-Control": "no-store"}


# -- signing in ---------------------------------------------------------------


@router.get("/login", summary="Sign in, or create the password")
async def login_form(
    request: Request,
    jukebox: Juke,
    renderer: Render,
    target: Annotated[str, Query(alias="next", max_length=200)] = _RETURN,
) -> Response:
    """The sign-in page, or the first-run page when there is no credential yet.

    One route for both, because the question the browser is asking is the same one — "how does
    an administrator get in here" — and the answer turns on a single fact the appliance knows
    better than the template does: whether a hash exists. Two URLs would be two pages that look
    nearly alike plus a redirect to keep straight.
    """

    configured = await read(jukebox.appliance, jukebox.admin.is_configured)
    return renderer.response(
        "admin/login.html",
        {**_login_base(request, jukebox), "configured": configured, "next": _safe(target)},
        headers=_SHELL,
    )


@router.post("/login", summary="Exchange the password for a session")
async def login(
    request: Request,
    jukebox: Juke,
    renderer: Render,
    password: Annotated[str, Form()] = "",
    target: Annotated[str, Query(alias="next", max_length=200)] = _RETURN,
) -> Response:
    """Sign in, or set the password for the first time (ADR-013's two branches).

    A wrong password and an absent password are answered with the same page and the same
    sentence, and the limiter is `AdminService`'s rather than this handler's: five failed
    attempts in fifteen minutes lock the *appliance*, not an address, because ADR-007 forbids
    keeping per-client state and an attacker on the box's only network is indistinguishable
    from a guest on it.

    Two `except` arms, because `RateLimitError` is a sibling of `AuthenticationError` rather than
    a kind of it, and one clause would answer a lock with the wrong sentence. The status is the
    service's decision either way; what the handler owns is that a locked operator gets the login
    page — with the wait, and with the field they will need when the wait is over — rather than an
    error shell with nowhere to go.
    """

    if not password:
        return _rejected(request, renderer, "Enter the administrator password.")
    configured = await read(jukebox.appliance, jukebox.admin.is_configured)
    step = partial(jukebox.admin.setup if not configured else jukebox.admin.login, password)
    try:
        session = await read(jukebox.appliance, step)
    except RateLimitError as exc:
        return _rejected(
            request, renderer, _reason(exc), retry_after_seconds=exc.retry_after_seconds
        )
    except AuthenticationError as exc:
        return _rejected(request, renderer, _reason(exc))
    # The destination came from a query parameter, which is exactly what a redirect sink should
    # be alarmed about, and the sink is real: a `303` whose `Location` a caller supplied. What
    # makes it safe is `_safe`, which refuses every target that is not an absolute path on this
    # host — a scheme, an authority, a backslash, or a character a URL parser would delete — and
    # substitutes `/admin/dashboard`, a constant. CodeQL does not model that function, so the
    # claim is made where the code is and tested in
    # `tests/integration/test_admin_interface.py`::test_a_url_that_arrived_from_elsewhere_is_answered_with_a_place
    # and ::test_a_path_that_is_ours_is_kept_exactly, which send the hostile shapes and the real
    # ones and assert which of the two left the appliance.
    #
    # codeql[py/url-redirection] `_safe()` above is the filter; both tests above are the proof.
    # The marker is inert in the analyser version this repository uses, so the alert is filed
    # anyway and stands dismissed as a false positive — SECURITY.md says where and why.
    page = RedirectResponse(_safe(target), status_code=status.HTTP_303_SEE_OTHER)
    session_cookie(page, session, minutes=jukebox.config.admin.session_minutes)
    return page


# -- the screens --------------------------------------------------------------


@secure.get("", summary="The dashboard")
async def index() -> Response:
    return RedirectResponse(_RETURN, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@secure.get("/dashboard", summary="What is playing, what is waiting, and whether it is well")
async def dashboard(request: Request, jukebox: Juke, renderer: Render) -> Response:
    return await _screen(request, jukebox, renderer, "dashboard")


@secure.get("/queue", summary="Every request, and the two things an operator can do to it")
async def queue_page(request: Request, jukebox: Juke, renderer: Render) -> Response:
    return await _screen(request, jukebox, renderer, "queue")


@secure.get("/health", summary="The aggregate and each component's own words")
async def health_page(request: Request, jukebox: Juke, renderer: Render) -> Response:
    return await _screen(request, jukebox, renderer, "health")


@secure.get("/statistics", summary="Counters, leaderboard, and what the library has done")
async def statistics_page(request: Request, jukebox: Juke, renderer: Render) -> Response:
    return await _screen(request, jukebox, renderer, "statistics")


@secure.get("/configuration", summary="What the appliance is running with")
async def configuration_page(request: Request, jukebox: Juke, renderer: Render) -> Response:
    return await _screen(request, jukebox, renderer, "configuration")


@secure.get("/library", summary="The library's provenance and its counts")
async def library_page(request: Request, jukebox: Juke, renderer: Render) -> Response:
    return await _screen(request, jukebox, renderer, "library")


@secure.get("/logs", summary="The appliance's own records, newest first")
async def logs_page(
    request: Request,
    jukebox: Juke,
    renderer: Render,
    level: Annotated[str | None, Query(max_length=8)] = None,
    limit: Annotated[int, Query(ge=1, le=admin_views.LOG_LIMIT)] = admin_views.LOG_LIMIT,
) -> Response:
    """The tail of what this process logged.

    Filterable by level and by count, and by nothing else: there is no query by client, no
    address and no user agent in a line, because the log is the appliance talking about itself
    and ADR-007 is the reason it does not talk about who it was talking to.

    Unlike every other route here, this one does not cross to the appliance thread: reading a
    ring buffer is not touching a database, and `LogTail` is safe from either side by
    construction (`encore/services/logging_service.py`).
    """

    wanted = level.upper() if level and level.upper() in admin_views.LOG_LEVELS else None
    return await _render(
        request,
        jukebox,
        renderer,
        "logs",
        admin_views.logs_context(jukebox, level=wanted, limit=limit),
    )


@secure.get("/password", summary="Change the password")
async def password_page(request: Request, jukebox: Juke, renderer: Render) -> Response:
    """The form, and the two facts that make it worth filling in.

    `last_login` is here because "when did someone last sign in" is the question an operator who
    has just been bounced to a login page asks, and it is the only history this appliance keeps
    about the administrator — one timestamp, no addresses, nothing about who was connecting
    while they were (ADR-007, ADR-013).
    """

    return await _render(
        request,
        jukebox,
        renderer,
        "password",
        {
            "configured": await read(jukebox.appliance, jukebox.admin.is_configured),
            "last_login": await read(jukebox.appliance, jukebox.admin.last_login),
            "minimum": MIN_PASSWORD_BYTES,
            "minutes": jukebox.config.admin.session_minutes,
        },
    )


# -- the things an operator can do --------------------------------------------


@secure.post("/playback/{command}", summary="Pause, resume, skip, stop, or start")
async def playback(
    *,
    request: Request,
    command: Annotated[str, Path(pattern="^(pause|resume|skip|stop|start)$")],
    jukebox: Juke,
    renderer: Render,
    panel: Panel = "dashboard",
    htmx: Htmx = None,
) -> Response:
    """One of five transport commands, answered with the panel it changed.

    `start` is here because a stopped appliance with a queue full of requests needs a way to
    begin, and SAPRS 8.8 lists starting among the administrative operations. It is
    `QueueService.play_next()`, not a new idea: the same advance the queue performs when a song
    finishes, run once by hand. It is also the only one of the five that can legitimately do
    nothing, which is why the answer is the panel rather than a claim.
    """

    # The command runs first, and in both languages: a handler that performed it only on the
    # branch it happened to write first is the shape of the bug this route had once, when the
    # HTMX path re-rendered the panel without touching the appliance. Pressing Pause must pause
    # whether or not the browser can swap markup.
    await read(jukebox.appliance, partial(_command, jukebox, command))
    if not htmx:
        return RedirectResponse(_RETURN, status_code=status.HTTP_303_SEE_OTHER)
    return await _changed(request, jukebox, renderer, panel, action=_sentence(command))


@secure.post("/queue/clear", summary="Take every waiting request out")
async def queue_clear(
    request: Request,
    jukebox: Juke,
    renderer: Render,
    panel: Panel = "dashboard",
    htmx: Htmx = None,
) -> Response:
    """Clear the queue, leaving the current track alone (SAPRS 8.8's "remove from queue").

    The one administrative action that is not undoable, which is why its button carries
    `hx-confirm` — and why an operator without JavaScript, who therefore gets no confirm
    dialog, is sent back to a dashboard that shows an empty Up Next rather than being asked "are
    you sure" by a page that cannot enforce the answer.
    """

    await read(jukebox.appliance, jukebox.queue.clear)
    if not htmx:
        return RedirectResponse(_RETURN, status_code=status.HTTP_303_SEE_OTHER)
    return await _changed(request, jukebox, renderer, panel, action="cleared the queue")


@secure.post("/queue/{item_id}/remove", summary="Take one request out")
async def queue_remove(
    *,
    request: Request,
    item_id: Annotated[int, Path(ge=1)],
    jukebox: Juke,
    renderer: Render,
    panel: Panel = "dashboard",
    htmx: Htmx = None,
) -> Response:
    """Remove one item. Removing what is playing is a skip, and `QueueService` says so.

    No confirmation step: the operation is reversible in the only sense it has — the song is
    still in the library and anyone can queue it again — and a modal between an operator and a
    mistake they can undo is a delay rather than a safety. An item that has already gone is a
    404 rather than a success, because a button on a screen that was thirty seconds old is the
    one way this action can be silently wrong.
    """

    removed = await read(jukebox.appliance, partial(jukebox.queue.remove, QueueItemId(item_id)))
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "that request is no longer in the queue")
    if not htmx:
        return RedirectResponse(_RETURN, status_code=status.HTTP_303_SEE_OTHER)
    return await _changed(request, jukebox, renderer, panel, action="Removed that request")


@secure.post("/library/reload", summary="Reopen the library database")
async def library_reload(
    request: Request,
    jukebox: Juke,
    renderer: Render,
    htmx: Htmx = None,
) -> Response:
    """Notice a rebuilt `library.db` without restarting the appliance.

    ADR-006's rule is that the runtime never writes the library, and this is the read side of
    the same bargain: one new handle on whatever file is there now, verified before the swap, so
    a build interrupted mid-write leaves the running appliance on the old handle rather than on
    half a file. `LibraryReloaded` is what tells every other screen that its idea of the library
    — including a "search is not available" sentence — may now be out of date.
    """

    fact = await read(jukebox.appliance, jukebox.library.reload)
    if not htmx:
        return RedirectResponse("/admin/library", status_code=status.HTTP_303_SEE_OTHER)
    return await _changed(
        request,
        jukebox,
        renderer,
        "library",
        action=f"Reopened the library, which now holds {fact.song_count} songs",
    )


@secure.post("/password", summary="Replace the password and end every session")
async def password_change(
    request: Request,
    jukebox: Juke,
    renderer: Render,
    current: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
) -> Response:
    """Change the credential. Every session dies, including this one (ADR-013).

    The answer is the login page rather than a confirmation panel, because the operator's next
    action is determined by the change: they are signed out, and a screen that said "done" while
    refusing their next click would be the interface's fault rather than theirs. A wrong current
    password is `AdminService`'s `AuthenticationError`, which for a browser means the redirect
    `encore/api/errors.py` explains.
    """

    await read(
        jukebox.appliance,
        partial(jukebox.admin.change_password, current=current, new=new_password),
    )
    page = renderer.response(
        "admin/login.html",
        {
            **_login_base(request, jukebox),
            "configured": True,
            "notice": "Password changed. Sign in with it to continue.",
            "next": _RETURN,
        },
        headers=_SHELL,
    )
    clear_session(page)
    return page


@secure.post("/logout", summary="End this session")
async def logout(request: Request, jukebox: Juke, htmx: Htmx = None) -> Response:
    """Forget this session here and now, and hand the browser back an empty cookie.

    `AdminService.logout` is the whole of it: the token leaves the live map, so the next request
    that presents it is anonymous again. The one-administrator consequence — signing out on the
    phone signs out the laptop — is ADR-013's, and it is also why there is no "sign out
    everywhere" button to be confused with this one.
    """

    await read(jukebox.appliance, partial(jukebox.admin.logout, session_token(request)))
    if not htmx:
        page = RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        clear_session(page)
        return page
    ack = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session(ack)
    ack.headers["HX-Redirect"] = "/admin/login"
    return ack


# -- shared shapes ------------------------------------------------------------


async def _screen(request: Request, jukebox: Jukebox, renderer: Renderer, name: str) -> Response:
    """A screen, assembled the one way it is ever assembled: on the appliance thread."""

    return await _render(
        request,
        jukebox,
        renderer,
        name,
        await read(jukebox.appliance, partial(PANELS[name], jukebox)),
    )


async def _render(
    request: Request,
    jukebox: Jukebox,
    renderer: Renderer,
    name: str,
    context: dict[str, Any],
) -> Response:
    """A whole shell, or the one region inside it, according to who asked.

    The shell choice is in the template rather than here: `admin/pages/*.html` each extend
    `admin/shell.html`, so there is no second argument that could disagree with the page about
    what wraps it.

    `name` picks `admin/pages/{name}.html` for a browser and `admin/panels/{name}.html` for an
    `HX-Request`, which is the convention `browse.py` and `panels.py` already share rather than a
    new invention. The alternative — a second set of URLs under `/admin/fragments/` — would put
    the same markup behind two addresses and let them drift.

    Both get the same context, which is the point of building it once here: an operator who
    refreshes a region and then navigates to the page should not see a number move, and the only
    way to guarantee that is for there to be one assembly.
    """

    base: dict[str, Any] = {
        "request": request,
        "nav": name,
        "domain": jukebox.config.server.domain,
        "version": jukebox.version,
        "is_admin": True,
        "health": jukebox.health.snapshot,
        "session": getattr(request.state, "admin_session", None),
        **context,
    }
    if _is_fragment(request):
        return renderer.response(f"admin/panels/{name}.html", base, headers=FRAGMENT_HEADERS)
    return renderer.response(f"admin/pages/{name}.html", base, headers=_SHELL)


async def _changed(
    request: Request, jukebox: Jukebox, renderer: Renderer, panel: str, *, action: str
) -> Response:
    """The region that asked, rebuilt after the change, with a line about what happened.

    No new fact is announced here. What moved the queue has already gone out over SSE to every
    guest (ADR-004); what is left to say is to the one screen that asked, and saying it by
    re-rendering the same builder the page uses is what keeps an operator's dashboard and a
    guest's phone describing one queue rather than two that agree by luck.
    """

    context = await read(jukebox.appliance, partial(PANELS[panel], jukebox))
    # The sentence about what just happened travels in the region it is drawn into, because a
    # fragment swap replaces the region that said it last time: a notice that outlived the state
    # it described would be the interface lying about a queue it has already changed.
    context["action"] = action
    return await _render(request, jukebox, renderer, panel, context)


def _login_base(request: Request, jukebox: Jukebox) -> dict[str, Any]:
    """The parts of the login page that are not about signing in (the shell's own facts)."""

    return {
        "request": request,
        "domain": jukebox.config.server.domain,
        "version": jukebox.version,
        "health": jukebox.health.snapshot,
        "is_admin": False,
        "error": None,
        "notice": None,
    }


def _sentence(command: str) -> str:
    """What the panel says after a transport button, in a sentence rather than a verb.

    The routes write their own sentences everywhere else, and this is the one place a word
    arrives from a URL, where `?panel=`-style identifiers and operator-facing English are the
    same string and must not be confused: `skip` becomes "Skipped", which is what happened, and
    not a command echoed back.
    """

    return {
        "pause": "Paused",
        "resume": "Resumed",
        "skip": "Skipped to the next request",
        "stop": "Stopped",
        "start": "Started the next request",
    }[command]


def _command(jukebox: Jukebox, command: str) -> object:
    """Perform a transport command through the queue, the only mutator there is (ADR-011)."""

    method = getattr(jukebox.queue, COMMANDS[command])
    return method()


def _rejected(
    request: Request,
    renderer: Renderer,
    reason: str,
    *,
    retry_after_seconds: int | None = None,
) -> Response:
    """Say no to a sign-in, in whichever language the caller used.

    A browser gets the login page back with the sentence in it, at 401 rather than 200 so a
    proxy and a test can tell the two apart; anything else gets the JSON envelope SAPRS 10.7
    describes. The password is never echoed, and nothing about the machine it came from is
    recorded: a failed sign-in is a fact about the appliance, not about a guest.

    `retry_after_seconds` changes the status as well as the wording. "That was wrong" and "not
    yet" are different answers — a client that hears the first one tries a different password and
    a client that hears the second one waits — and SAPRS 10.7 has a code for the second one. It is
    not a hint about the password, so it costs nothing that the uniform-refusal rule is guarding:
    a locked appliance is locked whether or not the last guess was close.
    """

    failure = RATE_LIMITED if retry_after_seconds is not None else UNAUTHORIZED
    if _is_fragment(request) or _is_html(request):
        headers = dict(_SHELL)
        if retry_after_seconds is not None:
            headers["Retry-After"] = str(retry_after_seconds)
        return renderer.response(
            "admin/login.html",
            {**_login_base(request, _jukebox(request)), "error": reason, "next": _RETURN},
            status_code=failure.status,
            headers=headers,
        )
    if retry_after_seconds is not None:
        # Numbers only. `details` is what a client branches on and `message` is what it shows, and
        # the minutes-in-a-sentence form of this answer is for the person reading the page above.
        return respond(request, failure, {"retry_after_seconds": retry_after_seconds})
    return respond(request, failure, {"detail": reason})


def _reason(exc: AdminError) -> str:
    """What an operator reads after a refusal, with the lock's wait in it when there is one.

    A wrong password and a locked appliance are different sentences because they want different
    actions — check what you typed, or wait — and SAPRS 10.8's rule is that a person must be able
    to act on what they are told. The count of remaining attempts is not printed: it would tell
    an attacker how much room they have left, and tell the operator nothing they can use.
    """

    if isinstance(exc, RateLimitError):
        minutes = exc.retry_after_seconds // 60 + 1
        return (
            f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}."
        )
    return "That is not the administrator password."


def _safe(target: str) -> str:
    r"""A place inside this appliance, or the dashboard.

    `?next=` arrives from a URL an operator may have followed, and a redirect that accepted
    `//evil.example` would turn the sign-in page into an open redirect on a box that only ever
    speaks to its own LAN. Three shapes have to be refused, and two of them are easy to miss:

    * `//host` is protocol-relative in every browser, and `scheme://host` needs no help.
    * `/\host` is the same thing in the browsers that normalise a backslash before parsing,
      which is most of them. A backslash is therefore not a path character here at all.
    * A tab or a newline is stripped by a URL parser *before* it decides what the target is, so
      `/%09/evil.example` — percent-decoded by the framework into a literal tab — becomes
      `//evil.example` on the way out. The class check below is what refuses it: an absolute path
      made only of printable, non-space characters cannot contain one.

    Everything else is accepted as a path on this host, including a query string, because the
    pages worth returning to (`/admin/queue`, `/admin/logs?level=warning`) are what an operator
    was doing before the session ended.
    """

    if not _PATH_ONLY.fullmatch(target) or target.startswith("//") or "\\" in target:
        return _RETURN
    return target


#: An absolute path, and nothing that a URL parser would reinterpret on the way out: no scheme,
#: no authority, no whitespace, no control character, no quote, and no byte above 0x7e (which
#: would have to be encoded before it could be a header value at all).
_PATH_ONLY: Final = re.compile(r"/[\x21-\x7e]*")


def _is_fragment(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _is_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _jukebox(request: Request) -> Jukebox:
    """The appliance, reached from the handler that already has the request.

    `_rejected` is called from two routes that both take `jukebox` as a dependency; threading it
    through as an argument would be the same value with one more parameter, and this function is
    the one line that says where it came from.
    """

    jukebox: Jukebox = request.app.state.jukebox
    return jukebox
