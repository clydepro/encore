"""Who is allowed to administer this appliance, and how the request says so (SAPRS 10.8, ADR-013).

The verification rule is SAPRS 10.8's — guest endpoints need nothing, administrative ones
need everything — and this module is the one place that turns it into a dependency a route
can declare. It is small on purpose: the *policy* lives in `encore/services/admin_service.py`
and the credential lives in `runtime.db`, so what is left here is a cookie, an origin check
and a `Depends`.

Four decisions worth the comments where they happen:

* **The session rides in a cookie, and the check runs on the event loop.** ADR-013 keeps the
  session map in memory precisely so that answering "are you allowed?" costs a dictionary
  lookup rather than a round trip to the one thread that owns the database. A route that
  needs the credential itself (login, password change) crosses `read()`; this does not.
* **`HttpOnly`, `SameSite=Strict`, no `Secure`.** The first two are the cross-site request
  defence, in place of a CSRF token (see ADR-013's consequences). The third is omitted
  because SAPRS 13.1's appliance serves plain HTTP on a LAN and a `Secure` cookie would
  make the administrative interface unreachable rather than unsafe.
* **A cross-site request is refused even with a valid cookie.** A `SameSite` attribute is a
  browser instruction, not a law, and `Origin` is one header to compare when a client sent
  one. It is checked only when present, because a `curl` with a cookie and no `Origin` is
  the documented way to script this appliance.
* **A person is redirected and a machine is refused.** The same split `POST /queue` makes:
  a browser that followed a link to `/admin/logs` without a session wants the login page,
  and `docs/api/README.md` promises a 401 to everything else. The mapping lives in
  `encore/api/errors.py`, because it is an answer shape rather than an auth rule.

Nothing here stores, logs or derives anything about a *guest*: no address, no user agent, no
first-seen. The one identity in this file belongs to the one administrator, and it is random
(SAPRS 1.3's "guest anonymity must not be compromised by the administrative interface").
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlparse

from fastapi import Depends, Request, Response

from encore.api.deps import Jukebox, jukebox_of
from encore.services.admin_service import AdminSession
from encore.services.errors import AuthenticationError, CrossSiteError

__all__ = [
    "SESSION_COOKIE",
    "RequireAdmin",
    "clear_session",
    "depend_admin",
    "session_cookie",
    "session_token",
]

#: The cookie name. Says what it is, because an operator reading a request log should not
#: have to open this file to know which of four cookies ended a login.
SESSION_COOKIE = "encore_admin"

#: The path a state-changing request must have come from, when it says. `None` means the
#: client sent no `Origin` at all, which is legitimate for a tool and impossible for a
#: cross-site form.
_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def session_token(request: Request) -> str | None:
    """The token this browser presented, if it presented one.

    Read rather than parsed: a cookie the appliance did not issue is not a session, and
    `AdminService.session()` is the only thing allowed to decide that.
    """

    token = request.cookies.get(SESSION_COOKIE)
    return token or None


def session_cookie(response: Response, session: AdminSession, *, minutes: int) -> None:
    """Attach a fresh session cookie to a response.

    `Max-Age` and the service's own expiry are set from the same number, which is the only
    way the two cannot disagree — and the reason both exist is that the browser needs one to
    know when to stop sending it and the appliance needs the other to know when to stop
    accepting it. A client that kept an old cookie and sent it later gets a 401, not a
    resurrection.
    """

    response.set_cookie(
        SESSION_COOKIE,
        session.token,
        max_age=minutes * 60,
        path="/",
        httponly=True,
        samesite="strict",
        secure=False,
    )


def clear_session(response: Response) -> None:
    """Withdraw the cookie. Pairs with `session_cookie`, and deletes by name and path."""

    response.delete_cookie(SESSION_COOKIE, path="/")


def depend_admin(
    request: Request, jukebox: Annotated[Jukebox, Depends(jukebox_of)]
) -> AdminSession:
    """The signed-in administrator behind this request.

    A dependency, so every administrative route says so in its own signature and the
    requirement is visible in the OpenAPI document rather than discovered at runtime.

    Raises:
        CrossSiteError: A state-changing request whose `Origin` is not this appliance.
        AuthenticationError: No session, or one that has run out.
    """

    _refuse_cross_site(request)
    session = jukebox.admin.session(session_token(request))
    if session is None:
        raise AuthenticationError("the administrator must sign in first")
    # Kept on the request as well as returned, because the shell wants to say when the
    # session it is rendering will end, and a route that took `Admin` as an argument only to
    # pass it to a template would be a route with a template dependency in its signature.
    request.state.admin_session = session
    return session


#: The annotated form routes use, so `Admin = Annotated[AdminSession, Depends(depend_admin)]`
#: reads as a type of the request rather than as a call.
RequireAdmin = Annotated[AdminSession, Depends(depend_admin)]


def _refuse_cross_site(request: Request) -> None:
    """Refuse a state-changing request that arrived from somewhere else.

    Compares origins rather than trusting `Sec-Fetch-Site`, because the former is universal
    and the latter is only in newer browsers — and sends nothing on a same-origin `GET`, so
    the check is skipped for the methods that cannot change anything anyway.
    """

    if request.method not in _UNSAFE:
        return
    origin = request.headers.get("origin")
    if origin is None:
        return
    if _host(origin) == _host(str(request.url)):
        return
    raise CrossSiteError("this request did not come from the appliance")


def _host(value: str) -> str:
    """The `host:port` of a URL or an `Origin`, which is the whole identity of an origin.

    A deployment behind a reverse proxy that rewrites `Host` is a deployment where this
    comparison fails closed — an administrator gets a 403 rather than a silent bypass — and
    SAPRS 13.1 does not support a proxy in front of Encore, so failing closed is right.
    """

    parsed = urlparse(value if "://" in value else f"//{value}")
    return (parsed.netloc or "").lower()
