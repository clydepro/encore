"""One error shape for two interfaces (SAPRS 10.6, 10.7, AEP 15).

`docs/api/README.md` fixes the JSON envelope — `{"error": {"code", "message",
"details"}}` — and SAPRS 10.7 fixes which status means what. This module is where a
Python exception becomes those two things, and it is one file rather than a `try:` in
each route because a mapping spread across eleven handlers is a mapping that disagrees
with itself somewhere.

The rule that gives the module its shape: **an HTMX request is answered in kind.** A
fragment endpoint's failure is still a fragment endpoint (SAPRS 10.6), so a browser
asking for `/fragments/up-next` gets a `<div>` that says the queue could not be read,
swapped into place — no dialog, no stack trace, no dead panel. `GET /api/v1/queue` gets
the envelope. A person who typed the URL gets the same sentence inside the page shell.
All three come from one function, so they cannot tell three different stories about one
failure.

What reaches `details` is chosen, never copied: the number a client can act on (the
queue's ceiling, the id that is missing) and nothing else — not a path, not a
connection string, not an exception chain (AEP 15). Two of them are said twice, once in
the body and once in a header (`Retry-After`), because a header is the half a client that
does not parse JSON can still obey.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from encore.api.html import Renderer
from encore.repositories.errors import (
    ContractViolationError,
    LibraryContractError,
    RuntimeContractError,
    StoreNotFoundError,
)
from encore.search.errors import SearchUnavailableError
from encore.services.errors import (
    AuthenticationError,
    CredentialExistsError,
    CrossSiteError,
    QueueFullError,
    RateLimitError,
    SongNotAvailableError,
    WeakCredentialError,
)
from encore.utilities.appliance import ApplianceNotRunning, WrongThreadError

__all__ = [
    "INVALID_REQUEST",
    "ErrorBody",
    "ErrorResponse",
    "register_error_handlers",
    "respond",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class Failure:
    """One kind of bad news: what to say, how loud to be, what code to give it."""

    status: int
    code: str
    message: str

    @property
    def is_server_fault(self) -> bool:
        return self.status >= status.HTTP_500_INTERNAL_SERVER_ERROR


QUEUE_FULL = Failure(
    status=status.HTTP_409_CONFLICT, code="queue_full", message="the queue is full"
)
SONG_NOT_AVAILABLE = Failure(
    status=status.HTTP_404_NOT_FOUND,
    code="song_not_available",
    message="that song is not in the library",
)
SEARCH_UNAVAILABLE = Failure(
    status=status.HTTP_503_SERVICE_UNAVAILABLE,
    code="search_unavailable",
    message="search is not available on this appliance",
)
NOT_READY = Failure(
    status=status.HTTP_503_SERVICE_UNAVAILABLE,
    code="not_ready",
    message="the appliance is not ready yet",
)
LIBRARY_UNUSABLE = Failure(
    status=status.HTTP_503_SERVICE_UNAVAILABLE,
    code="library_unavailable",
    message="that library file is not one this server can read; the running library is unchanged",
)
INVALID_REQUEST = Failure(
    status=422,
    code="invalid_request",
    message="the request was not valid",
)
UNAUTHORIZED = Failure(
    status=status.HTTP_401_UNAUTHORIZED,
    code="unauthorized",
    message="the administrator must sign in",
)
FORBIDDEN = Failure(
    status=status.HTTP_403_FORBIDDEN,
    code="forbidden",
    message="that request did not come from this appliance",
)
RATE_LIMITED = Failure(
    status=status.HTTP_429_TOO_MANY_REQUESTS,
    code="rate_limited",
    message="too many failed sign-in attempts",
)
CREDENTIAL_EXISTS = Failure(
    status=status.HTTP_409_CONFLICT,
    code="credential_exists",
    message="this appliance already has an administrator",
)
WEAK_CREDENTIAL = Failure(
    status=422,
    code="weak_credential",
    message="that password is too short to protect a shared appliance",
)
INTERNAL = Failure(
    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    code="internal",
    message="the appliance could not do that",
)

#: Each exception Encore raises on purpose, and what a client is told.
#:
#: `SongNotAvailableError` is 404 and not 409 because `encore/services/errors.py`
#: distinguishes "try something else" from "try later" — and a 409 on a song that was
#: never in the library would tell a guest to wait for a rebuild that cannot help.
BY_EXCEPTION: tuple[tuple[tuple[type[Exception], ...], Failure], ...] = (
    ((QueueFullError,), QUEUE_FULL),
    ((SongNotAvailableError,), SONG_NOT_AVAILABLE),
    ((SearchUnavailableError,), SEARCH_UNAVAILABLE),
    ((ApplianceNotRunning, WrongThreadError), NOT_READY),
    ((AuthenticationError,), UNAUTHORIZED),
    ((CrossSiteError,), FORBIDDEN),
    ((RateLimitError,), RATE_LIMITED),
    ((CredentialExistsError,), CREDENTIAL_EXISTS),
    ((WeakCredentialError,), WEAK_CREDENTIAL),
    # A reload is the one request-time path that opens `library.db` afresh, so the store's
    # refusals can now arrive at a route (SAPRS 11.9: the button must not be able to stop the
    # party, which it cannot — the previous handle stays current — but the page has to say so).
    # Registered by class rather than folded into a 500 because an unreadable library is an
    # operator's problem with an action attached to it, not a bug in this process.
    ((LibraryContractError, StoreNotFoundError, RuntimeContractError), LIBRARY_UNUSABLE),
    ((ContractViolationError,), INTERNAL),
)

#: The status codes SAPRS 10.7 names, so an `HTTPException(405)` raised by a router says
#: the same kind of word as a service that raised.
BY_STATUS: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "invalid_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    422: "invalid_request",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "internal",
    status.HTTP_503_SERVICE_UNAVAILABLE: "unavailable",
}


class ErrorBody(BaseModel):
    """The envelope `docs/api/README.md` promises, as a model rather than folklore."""

    code: str = Field(description="Stable machine-readable code, for example `queue_full`.")
    message: str = Field(description="One sentence for a person. Never a stack trace.")
    details: dict[str, Any] = Field(
        default_factory=dict, description="Values a client can act on: a limit, an id."
    )


class ErrorResponse(BaseModel):
    """A JSON error response."""

    error: ErrorBody

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def register_error_handlers(app: FastAPI, *, logger: logging.Logger | None = None) -> None:
    """Attach the mapping to an app. Called once, by `create_app`."""

    log = logger or logging.getLogger("encore.http")

    def handler_for(failure: Failure) -> Any:
        async def handle(request: Request, exc: Exception) -> Response:
            if failure.is_server_fault:
                log.warning(
                    "request failed",
                    extra={"code": failure.code, "path": request.url.path},
                    exc_info=exc,
                )
            return respond(request, failure, details_for(exc))

        return handle

    for exceptions, failure in BY_EXCEPTION:
        for exception_type in exceptions:
            app.add_exception_handler(exception_type, handler_for(failure))

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> Response:
        return respond(request, INVALID_REQUEST, {"fields": _field_problems(exc)}, fallback="json")

    # The Starlette class, not FastAPI's. `MethodNotAllowed` — the 405 SAPRS 10.7 names, and
    # the one failure a guest can cause just by reloading a page at the wrong moment — is
    # raised by the router against its own parent class, so registering only FastAPI's
    # subclass would let it through as a bare `{"detail": …}`.
    @app.exception_handler(StarletteHTTPException)
    async def http(request: Request, exc: StarletteHTTPException) -> Response:
        failure = Failure(
            status=exc.status_code,
            code=BY_STATUS.get(exc.status_code, "error"),
            message=str(exc.detail),
        )
        return respond(request, failure, {})

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> Response:
        # AEP 15: logged in full, explained in none. A request id would help an
        # operator and cost a header; the journal's timestamp plus the path is what
        # milestone 14's log tail will make searchable.
        # Starlette hands an exception to a *function* rather than to an `except` block, so
        # the checker cannot see that this is the handler; `exc_info` is what `log.exception`
        # would have set (AEP 15: logged in full, explained in none).
        log.error(
            "unhandled error",
            exc_info=exc,
            extra={"path": request.url.path},
        )
        return respond(request, INTERNAL, {})


#: A failure is as mutable as the state that produced it. SAPRS 10.10's "mutable state must
#: not be cached" has no exception for error pages, and a proxy that kept a "queue full" for
#: a guest would be a guest who could never add another song.
_NO_STORE = "no-store, max-age=0"
_HEADERS = {"Cache-Control": _NO_STORE}


def details_for(exc: Exception) -> dict[str, Any]:
    """The actionable numbers in one exception, and nothing else."""

    if isinstance(exc, QueueFullError):
        return {"limit": exc.limit}
    if isinstance(exc, SongNotAvailableError) and isinstance(exc.song_id, int):
        return {"song_id": int(exc.song_id)}
    if isinstance(exc, RateLimitError):
        # The one number a locked-out operator can act on, and the reason 429 has a
        # `Retry-After` in the protocol rather than a sentence.
        return {"retry_after_seconds": exc.retry_after_seconds}
    if isinstance(exc, AuthenticationError):
        # A fragment answer to an expired session has to say where to go, because the panel
        # that got the 401 cannot navigate itself (SAPRS 10.8's "no dead ends").
        return {"login": "/admin/login"}
    return {}


def respond(
    request: Request,
    failure: Failure,
    details: dict[str, Any],
    *,
    fallback: str = "auto",
) -> JSONResponse | HTMLResponse | RedirectResponse:
    """JSON for the API, a fragment for HTMX, the shell page for a person.

    `fallback` exists for one case: a validation failure happens before FastAPI knows
    which interface was meant, and a 422 is a developer-facing answer in any of the
    three. A test of the fragment path with a bad id should see the envelope too.

    One status is answered differently from the rest: an unauthenticated *person* following
    a link gets the login page (303), and everything else gets the 401 SAPRS 10.7 names.
    It is the same split `POST /queue` makes between a browser and a tool, and for the same
    reason — the answer a human wants is a place to be, and the answer a script wants is a
    code it can branch on. A 401 page would be a screen with a link on it, which is a worse
    interface than the link.

    The 401 that arrives from a panel is a third case, and it gets a door too: the fragment
    plus `HX-Redirect`, because the region cannot sign itself back in. A fragment answer also
    says `HX-Reswap: innerHTML`, because our regions swap themselves `outerHTML` and an error
    paragraph has neither the `id` nor the trigger that would make the next one arrive.
    """

    if failure.status == status.HTTP_401_UNAUTHORIZED and _is_person(request):
        return RedirectResponse(
            _door(request), status_code=status.HTTP_303_SEE_OTHER, headers=_HEADERS
        )
    headers = {**_HEADERS, **_advisory(details)}
    if _wants_fragment(request) and fallback != "json":
        # `innerHTML`, said explicitly, for a reason that is invisible from the server: an
        # administrative region is a self-refreshing element — it carries its own id and its own
        # hx-trigger — and the panels swap themselves outerHTML. Honouring that on a failure would
        # replace the region with `partials/error.html`, which carries neither, so one refused
        # rebuild would end that panel's ability to ever redraw again. Into it, not over it: the
        # sentence lands at the top of the last state the appliance had, and the trigger that
        # would replace that state is still in the document.
        headers["HX-Reswap"] = "innerHTML"
        if failure.status == status.HTTP_401_UNAUTHORIZED:
            # The one failure that is not an answer, it is a door. A fragment swapped into the
            # region would tell the operator the session ended and leave them standing in a
            # panel that cannot sign back in (an `hx-post` region's form is not the login form),
            # so the bridge is told to navigate instead — htmx reads `HX-Redirect` before it
            # decides anything about the body, which is why the body still says what happened
            # to a client that ignores the header.
            headers["HX-Redirect"] = _door(request)
        return HTMLResponse(
            _render(request, "partials/error.html", failure, details),
            status_code=failure.status,
            headers=headers,
        )
    if _is_api(request) or not _is_html(request):
        return JSONResponse(
            status_code=failure.status,
            content=ErrorResponse(
                error=ErrorBody(code=failure.code, message=failure.message, details=details)
            ).as_json(),
            headers=headers,
        )
    return HTMLResponse(
        _render(request, "error.html", failure, details),
        status_code=failure.status,
        headers=headers,
    )


def _door(request: Request) -> str:
    """Where a caller who is not signed in is sent, with the page they wanted attached."""

    return f"/admin/login?next={quote(request.url.path, safe='/')}"


def _advisory(details: Mapping[str, Any]) -> dict[str, str]:
    """The parts of a failure that are protocol rather than prose.

    One so far. A locked appliance knows *when* it will answer again, and a number that only
    appears in a JSON body is a number a proxy, a shell script and a browser's own retry logic
    all ignore. RFC 9110 §10.2.3 gives it a header; `details.retry_after_seconds` stays, because
    the envelope is where a client reads the reason and the header is where it obeys it.
    """

    seconds = details.get("retry_after_seconds")
    return {"Retry-After": str(seconds)} if isinstance(seconds, int) else {}


def _wants_fragment(request: Request) -> bool:
    """Whether the caller is HTMX, which is the only thing that says so (SAPRS 10.6)."""

    return request.headers.get("hx-request", "").lower() == "true"


def _is_person(request: Request) -> bool:
    """A browser asking for a page, which is the only caller a redirect helps.

    `HX-Request` is excluded on purpose: a fragment swap cannot follow a 303 into a login
    form usefully, so the panel gets the 401 fragment that tells the operator to sign in
    again, in the region it was going to draw.
    """

    return request.method == "GET" and not _wants_fragment(request) and _is_html(request)


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _is_html(request: Request) -> bool:
    """Whether a *person* asked, which is not the same question as "what will it parse".

    `Accept: */*` means "anything", and anything that says anything is a tool rather than a
    browser: a browser names `text/html` first because it is about to render it. Answering a
    tool with a document is how `curl` in a setup script gets a page, so `*/*` is JSON here
    and the HTML page is for the one header that means it (SAPRS 10.6's three audiences).
    """

    return "text/html" in request.headers.get("accept", "")


def _field_problems(exc: RequestValidationError) -> list[dict[str, str]]:
    """Pydantic's errors, reduced to what a client can fix.

    `ctx` is dropped on purpose: it can carry a limit value the response would rather
    not repeat, and `msg` already says it in a sentence.
    """

    return [
        {
            "location": ".".join(str(part) for part in item.get("loc", ())),
            "message": str(item.get("msg", "")),
        }
        for item in exc.errors()
    ]


def _render(request: Request, name: str, failure: Failure, details: dict[str, Any]) -> str:
    """Through the app's one `Renderer`, so an error is templated like everything else.

    The fallback is for a caller that built an app without templates — a unit test of the
    mapping, mostly — and exists so this module cannot be the reason such a test fails.
    """

    renderer: Renderer | None = getattr(request.app.state, "renderer", None)
    if renderer is None:  # pragma: no cover - create_app always sets it
        return f"<p>{failure.message}</p>"
    return renderer.render(name, {"request": request, "failure": failure, "details": details})
