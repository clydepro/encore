"""The administrator's credential and the sessions that follow from it (ADR-013, SAPRS 10.8).

These are the rules an HTTP test cannot check honestly: a lockout that lasts fifteen minutes,
a session that slides when it is used and dies when it is not, a password rotation that ends
every other tab. Asserting them against a real `runtime.db` and a `FixedClock` is what makes
them assertions rather than descriptions — an in-memory fake of `admin_state` would let the
service pass while the column it writes to did not exist.

Three things this file is careful to prove rather than assume:

* **The plaintext never reaches the file.** One test reads the bytes of `runtime.db` looking
  for the password, because the alternative is trusting that `Pbkdf2Hasher` is used for
  everything, and the reason ADR-013 exists is that a previous draft put a password in a YAML
  file where it would have been world-readable.
* **A refusal is one sentence.** Wrong password, missing credential and expired session all
  raise the same type with the same message, so nothing in the interface can leak which of
  the three a caller caused (AEP 15's argument about password oracles, applied to a LAN where
  every device is a potential attacker).
* **The state that is not in the database is deliberately so.** Sessions live in a dictionary,
  which is ADR-013's chosen trade — a restart signs everyone out, and the alternative is a
  token in `runtime.db` that survives a restore. The test that asserts a restart ends the
  sessions is the one that keeps that trade from being quietly reversed by someone adding a
  table.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from encore.config.models import AdminConfig
from encore.repositories.runtime import RuntimeStore, open_runtime_store
from encore.services.admin_service import (
    MAX_FAILURES,
    MAX_SESSIONS,
    AdminService,
    AdminSession,
)
from encore.services.errors import (
    AuthenticationError,
    CredentialExistsError,
    RateLimitError,
    WeakCredentialError,
)
from encore.utilities.clock import Clock

pytestmark = pytest.mark.unit

PASSWORD = "correct horse battery staple"  # pragma: allowlist secret
#: The other two strings a credential test needs, named so `detect-secrets` sees one line
#: each instead of a dozen that look like someone committed a password.
WRONG = "not it at all"  # pragma: allowlist secret
ROTATED = "a different long password"  # pragma: allowlist secret
START = datetime(2026, 7, 14, 22, 0, tzinfo=UTC)


class AdvancingClock(Clock):
    """Time, moved by the test rather than by the wall.

    Local rather than shared, in the shape `test_playback_service.py` uses: the two things a
    clock is asked for here are "what is it now" and "put it forward a quarter of an hour", and
    a support module with one implementation of each would be a support module two tests use.
    """

    def __init__(self, at: datetime = START) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def advance(self, by: timedelta) -> None:
        self.at += by


@pytest.fixture
def clock() -> AdvancingClock:
    return AdvancingClock()


@pytest.fixture
def runtime_path(tmp_path: Path) -> Path:
    return tmp_path / "runtime.db"


@pytest.fixture
def store(runtime_path: Path) -> Iterator[RuntimeStore]:
    """A real `runtime.db`, migrated, with the one row administration owns.

    `AdminStateRepository` is the writer this service is allowed to have (ADR-009), and a
    store built by the migration is the only way to know the table and the columns are the
    ones the installer ships.
    """

    opened = open_runtime_store(runtime_path)
    try:
        yield opened
    finally:
        opened.close()


def service(
    store: RuntimeStore, *, clock: AdvancingClock | None = None, minutes: int = 60
) -> AdminService:
    """An `AdminService` on a clock a test drives.

    A function rather than a fixture because almost every test here needs a different session
    lifetime, and a fixture with three parameters is a fixture nobody reads.
    """

    return AdminService(
        store=store, config=AdminConfig(session_minutes=minutes), clock=clock or AdvancingClock()
    )


# -- first run ---------------------------------------------------------------


def test_a_fresh_appliance_has_no_administrator(store: RuntimeStore) -> None:
    assert service(store).is_configured() is False


def test_the_first_password_becomes_the_only_one(store: RuntimeStore) -> None:
    admin = service(store)
    session = admin.setup(PASSWORD)

    assert admin.is_configured() is True
    assert session.token
    assert session.expires_at == START + timedelta(minutes=60)
    assert admin.login(PASSWORD) is not None


def test_a_second_setup_refuses_rather_than_replacing(store: RuntimeStore) -> None:
    """The answer is "someone already owns this box", not "unauthorised" (ADR-013).

    Confusing the two is how an operator who is locked out rebuilds the database and loses the
    party's queue to find out the credential was always there.
    """

    admin = service(store)
    admin.setup(PASSWORD)

    with pytest.raises(CredentialExistsError):
        admin.setup("something else entirely")
    assert admin.login(PASSWORD) is not None, "the refusal must not have written anything"


def test_a_password_too_short_for_a_shared_network_is_refused(store: RuntimeStore) -> None:
    admin = service(store)

    with pytest.raises(WeakCredentialError):
        admin.setup("short")
    assert admin.is_configured() is False


def test_the_plaintext_never_reaches_the_file(store: RuntimeStore, runtime_path: Path) -> None:
    """ADR-013's whole reason for existing, checked against the bytes on disk.

    A hash is stored, so the password is not; and because the hasher is PBKDF2 with a random
    salt, the file must not contain the hash of a guessable dictionary word either. This is a
    test of a *file*, which no unit test of a repository could be.
    """

    service(store).setup(PASSWORD)

    raw = runtime_path.read_bytes()
    assert PASSWORD.encode() not in raw
    assert b"correct" not in raw


# -- signing in --------------------------------------------------------------


def test_a_wrong_password_is_one_sentence_whatever_it_was_mistaken_for(
    store: RuntimeStore, tmp_path: Path
) -> None:
    """Two causes, one answer, asserted on the text rather than on the type.

    A caller that can tell "that is not the password" from "there is no password yet" has an
    oracle, and on a LAN every device is a potential caller (AEP 15).
    """

    admin = service(store)
    admin.setup(PASSWORD)
    unwritten = open_runtime_store(tmp_path / "another.db")
    try:
        with pytest.raises(AuthenticationError) as wrong:
            admin.login("almost the same but not")
        with pytest.raises(AuthenticationError) as missing:
            service(unwritten).login(PASSWORD)
    finally:
        unwritten.close()

    assert str(wrong.value) == str(missing.value)


def test_a_successful_sign_in_is_recorded_in_the_database(store: RuntimeStore) -> None:
    admin = service(store)
    admin.setup(PASSWORD)
    first = admin.last_login()
    assert first is None or first <= START

    admin.login(PASSWORD)
    assert admin.last_login() == START


def test_five_failures_lock_the_appliance_and_say_when_it_opens_again(
    store: RuntimeStore, clock: AdvancingClock
) -> None:
    """The limiter is per appliance, not per address (ADR-007), and so it must be *global*.

    `retry_after_seconds` is asserted because a 429 without a number is a 429 that sends an
    operator to a manual; the number is the answer.
    """

    admin = service(store, clock=clock)
    admin.setup(PASSWORD)

    for _ in range(MAX_FAILURES):
        with pytest.raises(AuthenticationError):
            admin.login("wrong password entirely")

    with pytest.raises(RateLimitError) as refused:
        admin.login(PASSWORD)
    assert refused.value.retry_after_seconds > 0


def test_a_success_clears_the_window(store: RuntimeStore, clock: AdvancingClock) -> None:
    admin = service(store, clock=clock)
    admin.setup(PASSWORD)
    for _ in range(MAX_FAILURES - 1):
        with pytest.raises(AuthenticationError):
            admin.login("no")

    assert admin.login(PASSWORD) is not None
    for _ in range(MAX_FAILURES - 1):
        with pytest.raises(AuthenticationError):
            admin.login("no")
    assert admin.login(PASSWORD) is not None, "a cleared window must not have carried over"


def test_the_window_expires(store: RuntimeStore, clock: AdvancingClock) -> None:
    admin = service(store, clock=clock)
    admin.setup(PASSWORD)
    for _ in range(MAX_FAILURES):
        with pytest.raises(AuthenticationError):
            admin.login("no")

    clock.advance(timedelta(minutes=16))
    assert admin.login(PASSWORD) is not None


# -- sessions ----------------------------------------------------------------


def test_a_session_is_the_token_not_the_password(store: RuntimeStore) -> None:
    admin = service(store)
    session = admin.setup(PASSWORD)

    assert admin.session(session.token) is not None
    assert admin.session(PASSWORD) is None
    assert admin.session(None) is None
    assert admin.session("") is None


def test_using_a_session_moves_its_deadline(store: RuntimeStore, clock: AdvancingClock) -> None:
    """Sliding expiry (ADR-013): the clock an operator is judged by is the last thing they did.

    An absolute deadline is how a dashboard signs a person out mid-way through clearing a
    jammed queue, which is the worst possible moment to lose the interface.
    """

    admin = service(store, clock=clock, minutes=30)
    session = admin.setup(PASSWORD)

    clock.advance(timedelta(minutes=20))
    held = admin.session(session.token)
    assert held is not None
    assert held.expires_at == START + timedelta(minutes=50)

    clock.advance(timedelta(minutes=45))
    assert admin.session(session.token) is None


def test_a_session_that_expires_is_gone_from_memory(
    store: RuntimeStore, clock: AdvancingClock
) -> None:
    admin = service(store, clock=clock, minutes=5)
    session = admin.setup(PASSWORD)
    assert len(admin.sessions()) == 1

    clock.advance(timedelta(minutes=6))
    assert admin.sessions() == ()
    assert admin.session(session.token) is None


def test_logout_ends_one_session_and_not_the_appliance(store: RuntimeStore) -> None:
    admin = service(store)
    first = admin.setup(PASSWORD)
    second = admin.login(PASSWORD)

    admin.logout(first.token)
    assert admin.session(first.token) is None
    assert admin.session(second.token) is not None
    admin.logout(first.token)  # idempotent, as a sign-out must be


def test_only_a_handful_of_sessions_exist_at_once(store: RuntimeStore) -> None:
    """The cap is eviction rather than refusal, because whoever is typing is the operator.

    This is a memory bound, not a security feature: one administrator, on one appliance, with
    a phone and a laptop open. A cap that refused a new sign-in would lock the operator out of
    a box they were using to fix a party.
    """

    admin = service(store)
    admin.setup(PASSWORD)
    tokens = [admin.login(PASSWORD).token for _ in range(MAX_SESSIONS + 4)]

    live = {session.token for session in admin.sessions()}
    assert len(live) <= MAX_SESSIONS
    assert tokens[-1] in live, "the newest session must never be the one evicted"


def test_a_restart_ends_every_session(store: RuntimeStore) -> None:
    """ADR-013's chosen cost, asserted so it cannot be reversed by an improvement.

    The alternative — a session table in `runtime.db` — survives a restore from backup, and a
    token that survives a restore is a token somebody can bring back. A restart signing the
    operator out is one keystroke; the failure it prevents is permanent.
    """

    admin = service(store)
    session = admin.setup(PASSWORD)
    del admin

    assert service(store).session(session.token) is None, "sessions must not be durable"


def test_changing_the_password_revokes_everything(store: RuntimeStore) -> None:
    admin = service(store)
    kept = admin.setup(PASSWORD)
    other = admin.login(PASSWORD)

    admin.change_password(current=PASSWORD, new=ROTATED)

    assert admin.session(kept.token) is None
    assert admin.session(other.token) is None
    assert admin.login(ROTATED) is not None


def test_changing_a_password_needs_the_old_one(store: RuntimeStore) -> None:
    admin = service(store)
    admin.setup(PASSWORD)

    with pytest.raises(AuthenticationError):
        admin.change_password(current=WRONG, new=ROTATED)
    assert admin.login(PASSWORD) is not None


def test_a_replacement_has_to_be_long_enough_too(store: RuntimeStore) -> None:
    admin = service(store)
    admin.setup(PASSWORD)

    with pytest.raises(WeakCredentialError):
        admin.change_password(current=PASSWORD, new="tiny")
    assert admin.login(PASSWORD) is not None


def test_sessions_are_reported_without_being_inspected(store: RuntimeStore) -> None:
    """The dashboard shows a count. It shows nothing else, because there is nothing else to
    show that would not be an identity (ADR-007's rule applies to the administrator too)."""

    admin = service(store)
    session: AdminSession = admin.setup(PASSWORD)

    listed = admin.sessions()
    assert [item.token for item in listed] == [session.token]
    assert listed[0].signed_in_at == START
