"""Administrative access: one credential, and the sessions that follow from it (SAPRS 1.3, 10.8, ADR-013).

AIG 7 lists this capability among the core services and SAPRS 10.8 makes it mandatory:
guest endpoints need no authentication, administrative ones need all of it. What the
specification does not say — and what ADR-013 exists to settle — is where the thing a
browser holds after a successful login *lives*. Here, in a dictionary, for four reasons
that add up:

* The credential itself is already decided and already stored. `admin_state` is a
  one-row table in `runtime.db` (SAPRS 5.7's "administrative state", ADR-009's write side,
  phase 2's `AdminStateRepository`). Nothing in this module hashes or stores a password;
  it asks that repository whether a password is right, which is the only place in the
  tree that can say so.
* A session that lived in `runtime.db` would put a *write* on the request path of an
  appliance with one writer thread (ADR-012), for a value that must not outlive the
  process. `runtime.db` holds state; a login is not state, it is permission with a
  deadline.
* Verification then costs no I/O, so an authorized page view costs the same reads an
  unauthorized one would have cost, and SAPRS 1.8's budgets stay the budgets.
* Revocation becomes a dictionary lookup. Logout, a password change and a restart are
  three ways of saying the same true thing rather than three mechanisms that can disagree
  about whether somebody is still signed in.

**Two threads, split by what they touch.** `login()`, `setup()` and `change_password()`
read or write `runtime.db` and belong on the appliance thread; `encore/api/` reaches them
through `read()` like every other service call. `session()` and `logout()` touch only the
in-process map and are safe to call from the event loop — which is what makes a session
check cost microseconds on a page view. The split is stated in the method docstrings
because a reviewer checking ADR-012 needs to see it, not infer it.

**What is deliberately absent:** any record of *who* is connected beyond the one
administrator, any per-client counter, and any identity attached to a request. The failure
limiter below counts failures for the appliance rather than per address, which is the
shape ADR-007 forces: a map keyed by source IP is the first column of a table that says
who asked, and this is the interface the specification says must not compromise guest
anonymity (SAPRS 1.3).
"""

from __future__ import annotations

import logging
import secrets
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from encore.config.models import AdminConfig
from encore.services.errors import (
    AuthenticationError,
    CredentialExistsError,
    RateLimitError,
    WeakCredentialError,
)
from encore.utilities.clock import Clock, SystemClock

__all__ = [
    "MIN_PASSWORD_BYTES",
    "AdminCredential",
    "AdminService",
    "AdminSession",
    "AdminStore",
    "AdminTransaction",
]

#: The shortest password the interface will accept. `AdminStateRepository` refuses an
#: empty one; this refuses a short one, because a four-character operator login on a LAN
#: appliance is a courtesy to whoever's laptop is next to it. Ten is roughly "a phrase",
#: and PBKDF2's cost makes an offline guess on top of it expensive rather than weekend-
#: sized (SAPRS 12.3).
MIN_PASSWORD_BYTES = 10

#: Concurrent sessions kept. One administrator signs in; sixteen means a script, and a
#: dictionary that grows with a script is a memory leak with a login form attached to it.
MAX_SESSIONS = 16

#: The failure window: this many wrong passwords in this long, then refuse to answer.
#: Global rather than per address by ADR-013's argument, and generous enough that a
#: Caps-Lock mistake at a party costs nothing.
MAX_FAILURES = 5
FAILURE_WINDOW = timedelta(minutes=15)

#: A token from `secrets`, URL-safe so it can live in a cookie without encoding. 32 bytes
#: is 256 bits, which is the length at which "guess it" stops being a plan.
TOKEN_BYTES = 32


@dataclass(frozen=True, slots=True, kw_only=True)
class AdminSession:
    """One signed-in operator, as the appliance remembers it.

    `token` is the bearer secret and the map key; `expires_at` is a deadline that slides
    forward on use, which is ADR-013's "60 idle minutes" rather than "60 minutes, however
    busy you are". Nothing here identifies a client: there is no address, no user agent,
    no first-seen. An admin session is a permission, not a record.
    """

    token: str
    signed_in_at: datetime
    expires_at: datetime

    # No `expires_within()` helper, deliberately, although one was written. A banner saying
    # "your session ends soon" is meaningful for an absolute deadline and is nonsense for a
    # sliding one: after fifty idle minutes the session has *less* than ten minutes left only if
    # nobody touches it, and the moment somebody does, the deadline it was warning about has
    # moved. What remains is a notice that can only ever be right about a tab nobody is looking
    # at. The case the banner was reaching for — an operator discovering mid-keystroke that they
    # are signed out — is answered where it is observable instead: the next request gets a 401,
    # and a 401 from a panel goes to the sign-in page with `?next=` back to it.


class AdminCredential(Protocol):
    """`AdminStateRepository`, as far as this service needs it to be.

    Declared structurally for the reason `QueueService` gives for `QueueWrites`: a
    service that imported the repository would import SQLAlchemy with it (ADR-009, and
    the guardrail test that reads these files to check exactly that).
    """

    def is_configured(self) -> bool: ...

    def verify(self, password: str) -> bool: ...

    def set_password(self, password: str) -> None: ...

    def record_login(self, *, at: datetime) -> None: ...

    def last_login(self) -> datetime | None: ...


class AdminTransaction(Protocol):
    """One `runtime.db` transaction, as far as administration reaches it."""

    @property
    def admin_state(self) -> AdminCredential: ...


class AdminStore(Protocol):
    """The transaction factory `RuntimeStore` satisfies (see `QueueStore` for the why)."""

    @contextmanager
    def unit_of_work(self) -> Iterator[AdminTransaction]: ...


class AdminService:
    """Verifies the credential, and remembers who is allowed in afterwards.

    Args:
        store: The runtime database, as a transaction factory. Every method that touches
            it is documented as doing so.
        config: `admin:` — the session lifetime. There is no password in it and there
            cannot be (SAPRS 12.6, and `extra="forbid"` makes the attempt a startup error).
        clock: Injectable, because expiry and the failure window are both arithmetic.
        logger: For the events an operator would want to find in the morning: a sign-in
            after a fresh install, a lockout, a credential change. Never a password.
    """

    def __init__(
        self,
        *,
        store: AdminStore,
        config: AdminConfig | None = None,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._config = config if config is not None else AdminConfig()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._logger = logger or logging.getLogger("encore.admin")
        self._sessions: dict[str, AdminSession] = {}
        self._failures: deque[datetime] = deque()

    # -- the credential (each of these reads or writes `runtime.db`) ---------

    def is_configured(self) -> bool:
        """Whether a credential exists yet — the first-run question (ADR-013)."""

        with self._store.unit_of_work() as work:
            return work.admin_state.is_configured()

    def login(self, password: str) -> AdminSession:
        """Verify `password` and, if it is right, open a session.

        A wrong password and an absent credential raise the same `AuthenticationError` with
        the same sentence: the difference between "no" and "no, and here is why" is the
        difference between a login form and a password oracle (AEP 15). A locked window is
        the one case worth a different exception, because "try later" is actionable and "no"
        is not (SAPRS 10.7's 429).

        Raises:
            AuthenticationError: There is no credential yet, or this is not the password.
            RateLimitError: The failure window is full. Checked before the hash, so a
                locked appliance does not spend 0.2 s of PBKDF2 per guess either.
        """

        self._refuse_if_locked()
        now = self._clock.now()
        with self._store.unit_of_work() as work:
            if not work.admin_state.is_configured():
                # The same sentence as a wrong password, deliberately. "This box has no
                # credential yet" is a fact an unauthenticated caller can use — it tells them
                # the first-run path is open — and the one place that is allowed to learn it is
                # the login page, which asks `is_configured()` of its own accord and shows the
                # setup form instead (ADR-013).
                raise AuthenticationError("the password was not right")
            ok = work.admin_state.verify(password)
            if ok:
                work.admin_state.record_login(at=now)
        if not ok:
            self._record_failure(now)
            raise AuthenticationError("the password was not right")
        self._failures.clear()
        session = self._mint(now)
        self._logger.info("administrator signed in", extra={"expires_at": session.expires_at})
        return session

    def setup(self, password: str) -> AdminSession:
        """Set the credential on a box that has none, and sign the caller in.

        This is the first-run path ADR-013 accepts with its eyes open: it is reachable
        exactly while `admin_state` has no row, logs a warning when it is used, and is
        superseded on any install milestone 15's installer performs, which seeds the
        credential from the environment before the service starts (SAPRS 12.6).

        Raises:
            CredentialExistsError: There is a credential. Not "unauthorized" — the
                answer is "someone already owns this box", and the two must not sound
                alike to the person who is locked out and trying to find out why.
            WeakCredentialError: Too short to type on a shared network.
        """

        if not self._acceptable(password):
            raise WeakCredentialError(_WEAK)
        with self._store.unit_of_work() as work:
            if work.admin_state.is_configured():
                raise CredentialExistsError("this appliance already has an administrator")
            work.admin_state.set_password(password)
        self._logger.warning(
            "the administrator credential was created over HTTP on a fresh appliance; "
            "set ENCORE_ADMIN_PASSWORD at install time instead (SAPRS 12.6, ADR-013)"
        )
        now = self._clock.now()
        self._failures.clear()
        return self._mint(now)

    def seed(self, password: str) -> bool:
        """Create the credential from the environment, if there is nothing to create it into.

        This is the path ADR-013 said milestone 15 would provide and ADR-015 implements: the
        appliance's first boot, with `ENCORE_ADMIN_PASSWORD` in its environment and no row in
        `admin_state`, takes the password and becomes configurable without a browser being
        pointed at it (SAPRS 12.6). Three things distinguish it from `setup`, and all three are
        the reason it is a separate method rather than a flag on one:

        * It is **idempotent**. A credential already present wins, always, and the environment
            variable is then a stale line in a file — which is what `encore-install verify`
            reports, not an exception at startup.
        * It **mints no session**. Nobody is waiting for a login, and a token issued to a
            process with no client attached would be a token in a log.
        * It **logs at INFO, not WARNING**. The warning in `setup` is about a password chosen
            over HTTP on a machine that may not be alone on its network; that is precisely what
            did not happen here, and a boot that warns about its own security every time the
            service restarts teaches an operator to ignore the one line worth reading.

        The value is never logged, never written to a file, and never kept on the appliance
        after this call returns.

        Args:
            password: Whatever `ENCORE_ADMIN_PASSWORD` held. Empty is not a request to seed.

        Returns:
            True if this boot created the credential, False if one already existed.

        Raises:
            WeakCredentialError: Too short to type on a shared network. Raised rather than
                ignored because the alternative is an appliance that boots with no
                administrator while its operator believes there is one.
        """

        if not password:
            return False
        if not self._acceptable(password):
            raise WeakCredentialError(_WEAK)
        with self._store.unit_of_work() as work:
            if work.admin_state.is_configured():
                return False
            work.admin_state.set_password(password)
        self._logger.info(
            "the administrator credential was seeded from the environment (SAPRS 12.6, ADR-015);"
            " the variable can be removed from the appliance now"
        )
        return True

    def change_password(self, *, current: str, new: str) -> None:
        """Rotate the credential, and revoke every session including this one.

        A rotation that left live sessions valid would be a rotation with a delay nobody
        can see, and "I changed the password and they are still in" is the sentence this
        prevents by not being possible.

        Raises:
            AuthenticationError: `current` is wrong.
            WeakCredentialError: `new` is too short.
        """

        if not self._acceptable(new):
            raise WeakCredentialError(_WEAK)
        with self._store.unit_of_work() as work:
            if not work.admin_state.verify(current):
                self._record_failure(self._clock.now())
                raise AuthenticationError("the current password was not right")
            work.admin_state.set_password(new)
        self._sessions.clear()
        self._failures.clear()
        self._logger.info(
            "the administrator credential changed; every session was revoked and must sign in again"
        )

    # -- the session (memory only, so the event loop may do it) --------------

    def session(self, token: str | None) -> AdminSession | None:
        """The live session behind `token`, with the deadline moved forward.

        Returns None for no token, a token this process never issued, and an expired one —
        the three answers a caller cannot usefully distinguish and should not, because
        telling them apart tells an attacker whether a guessed token once existed.

        Sliding rather than absolute (ADR-013): an operator mid-way through clearing a
        jammed queue should not be interrupted by a clock that started when they typed a
        password, and an operator who walked away is logged out by the same rule.
        """

        if not token:
            return None
        now = self._clock.now()
        found = self._sessions.get(token)
        if found is None:
            return None
        if found.expires_at <= now:
            del self._sessions[token]
            return None
        renewed = AdminSession(
            token=found.token,
            signed_in_at=found.signed_in_at,
            expires_at=now + timedelta(minutes=self._config.session_minutes),
        )
        self._sessions[token] = renewed
        return renewed

    def logout(self, token: str | None) -> None:
        """Forget a session. Idempotent, and correct for a token already expired."""

        if token is not None:
            self._sessions.pop(token, None)

    def sessions(self) -> tuple[AdminSession, ...]:
        """The live sessions, for the dashboard. Expired ones are pruned on the way out."""

        self._prune(self._clock.now())
        return tuple(self._sessions.values())

    def last_login(self) -> datetime | None:
        """When someone last got in, or None if nobody ever has (`runtime.db`).

        Worth a page: "the appliance was reinstalled at 4 a.m." and "someone logged in at
        4 a.m." are different sentences, and `admin_state.last_login_at` is the row that
        tells them apart without a log search.
        """

        with self._store.unit_of_work() as work:
            return work.admin_state.last_login()

    # -- internals ----------------------------------------------------------

    def _mint(self, now: datetime) -> AdminSession:
        """Issue a session, after forgetting any that have already run out.

        The cap is enforced by eviction of the session that expires first rather than by
        refusing the new one: whoever is typing a password right now is the more likely
        to be the operator.
        """

        self._prune(now)
        while len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda session: session.expires_at)
            del self._sessions[oldest.token]
        session = AdminSession(
            token=secrets.token_urlsafe(TOKEN_BYTES),
            signed_in_at=now,
            expires_at=now + timedelta(minutes=self._config.session_minutes),
        )
        self._sessions[session.token] = session
        return session

    def _prune(self, now: datetime) -> None:
        for token, session in list(self._sessions.items()):
            if session.expires_at <= now:
                del self._sessions[token]

    def _record_failure(self, now: datetime) -> None:
        self._failures.append(now)
        self._trim(now)

    def _trim(self, now: datetime) -> None:
        while self._failures and self._failures[0] <= now - FAILURE_WINDOW:
            self._failures.popleft()

    def _refuse_if_locked(self) -> None:
        now = self._clock.now()
        self._trim(now)
        if len(self._failures) < MAX_FAILURES:
            return
        oldest = self._failures[0]
        retry = oldest + FAILURE_WINDOW - now
        raise RateLimitError(max(1, int(retry.total_seconds())))

    def _acceptable(self, password: str) -> bool:
        return bool(password) and len(password.encode()) >= MIN_PASSWORD_BYTES


_WEAK = f"the administrator password must be at least {MIN_PASSWORD_BYTES} characters"
