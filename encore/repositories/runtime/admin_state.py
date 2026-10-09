"""`admin_state` — the single administrative credential (SAPRS 5.7, 12.3, 12.6).

One row, keyed so a second cannot exist. That is the schema enforcing SAPRS 12.3's
statement that there is one administrator, which is stronger than a service that
remembers to check: a migration, a restored backup or a hand-edited database all fail
against `CHECK (id = 1)` rather than quietly adding a second account nobody configured.

No credential comes from `config.yaml`. `AdminConfig` says so and AEP 17 repeats it: a
password in a file that is world-readable on a shared SD card, that ends up in a
support zip, and that survives every other change to the appliance is not a secret. The
hash lives here, and the first boot seeds it from `ENCORE_ADMIN_PASSWORD` (milestone
15).

Hashing is behind a protocol, defaulting to PBKDF2-HMAC-SHA256. SAPRS 12.3 names
"bcrypt or argon2id", and neither is a dependency yet; the installer may add one, and
the point of the protocol is that swapping it in does not touch this file, the table,
or the migration. The format string carries the algorithm and its parameters so an old
hash still verifies after the default changes — which is the only way a password hash
can be rotated without locking somebody out.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from encore.repositories.runtime.models import AdminState

__all__ = [
    "AdminStateRepository",
    "HashFormat",
    "PasswordHasher",
    "Pbkdf2Hasher",
    "parse_hash",
]

#: The delimiter in a stored hash. Chosen so that no base64 or hex alphabet contains
#: it, which is what makes the format parseable without a schema migration.
SEPARATOR: Final = "$"

#: `scheme$parameters$salt$digest` — four fields, and the count is the format's version.
#: A stored hash with any other shape is not a password this build can verify.
HASH_FIELDS: Final = 4
PBKDF2_ROUNDS: Final = 600_000
SALT_BYTES: Final = 16
DIGEST: Final = "sha256"
_MAX_VERIFY_BYTES: Final = 1024


class HashFormat(Protocol):
    """The parameters a stored hash declares. Structural, so a hasher can be a dataclass."""

    def scheme(self) -> str: ...

    def render(self, salt: bytes, digest: bytes) -> str: ...


@dataclass(frozen=True, slots=True)
class Pbkdf2Hasher:
    """PBKDF2-HMAC-SHA256, from the standard library (SAPRS 12.3's "or equivalent").

    Attributes:
        rounds: 600,000, which is OWASP's 2023 recommendation for PBKDF2-SHA256 and
            roughly 0.2 s on a Pi 4 — slow enough to make an offline guess expensive
            and fast enough that a login is not the thing that feels sluggish.
    """

    rounds: int = PBKDF2_ROUNDS
    digest: str = DIGEST
    salt_bytes: int = SALT_BYTES

    def hash(self, password: str) -> str:
        salt = secrets.token_bytes(self.salt_bytes)
        return self.render(salt, self._derive(password, salt))

    def verify(self, password: str, stored: str) -> bool:
        """Compare in constant time, and never raise.

        A malformed or unknown-scheme stored value answers False. Raising would let an
        attacker probe the hash format by reading the error, and would take an admin
        login down on a data problem it cannot fix.
        """

        try:
            scheme, parameters, salt, digest = parse_hash(stored)
        except ValueError:
            return False
        if scheme != "pbkdf2-" + self.digest or len(password.encode()) > _MAX_VERIFY_BYTES:
            return False
        try:
            rounds = int(parameters)
        except ValueError:
            return False
        candidate = hashlib.pbkdf2_hmac(self.digest, password.encode(), salt, rounds)
        return hmac.compare_digest(candidate, digest)

    def scheme(self) -> str:
        return f"pbkdf2-{self.digest}"

    def render(self, salt: bytes, digest: bytes) -> str:
        return SEPARATOR.join((self.scheme(), str(self.rounds), salt.hex(), digest.hex()))

    def _derive(self, password: str, salt: bytes) -> bytes:
        if len(password.encode()) > _MAX_VERIFY_BYTES:
            raise ValueError(f"a password longer than {_MAX_VERIFY_BYTES} bytes is not supported")
        return hashlib.pbkdf2_hmac(self.digest, password.encode(), salt, self.rounds)


class PasswordHasher(Protocol):
    """What the repository needs: `hash` and `verify`, no other surface."""

    def hash(self, password: str) -> str: ...

    def verify(self, password: str, stored: str) -> bool: ...


def parse_hash(stored: str) -> tuple[str, str, bytes, bytes]:
    """Split a stored hash into scheme, parameters, salt and digest.

    Raises:
        ValueError: Not four `$`-separated parts, or a hex field that is not hex. The
            caller turns that into a refused login.
    """

    parts = stored.split(SEPARATOR)
    if len(parts) != HASH_FIELDS:
        raise ValueError(f"expected {HASH_FIELDS} hash fields, got {len(parts)}")
    scheme, parameters, salt, digest = parts
    return scheme, parameters, bytes.fromhex(salt), bytes.fromhex(digest)


class AdminStateRepository:
    """Reads and writes the one admin row."""

    def __init__(self, session: Session, *, hasher: PasswordHasher | None = None) -> None:
        self._session = session
        self._hasher = hasher if hasher is not None else Pbkdf2Hasher()

    def _row(self) -> AdminState | None:
        return self._session.get(AdminState, 1)

    def is_configured(self) -> bool:
        return self._row() is not None

    def verify(self, password: str) -> bool:
        row = self._row()
        return row is not None and self._hasher.verify(password, row.password_hash)

    def set_password(self, password: str) -> None:
        """Store a new credential, hashing it here so no caller can store a plain one.

        Raises:
            ValueError: An empty password. SAPRS 12.3 assumes a password exists; the
                installer is the thing that asks for one, and the repository's job is
                to refuse to write the alternative.
        """

        if not password:
            raise ValueError("the administrator password cannot be empty")
        stored = self._hasher.hash(password)
        row = self._row()
        if row is None:
            self._session.add(AdminState(id=1, password_hash=stored))
        else:
            row.password_hash = stored
        self._session.flush()

    def record_login(self, *, at: datetime) -> None:
        """Stamp the last accepted login.

        Not on failure. A table that recorded failed attempts would be the thing an
        attacker fills up, and the log is where failed attempts belong (milestone 14).
        """

        self._session.execute(update(AdminState).where(AdminState.id == 1).values(last_login_at=at))

    def scheme(self) -> str:
        """The hash this build produces, for the configuration summary."""

        return self._hasher.scheme() if isinstance(self._hasher, Pbkdf2Hasher) else "external"

    def stored_scheme(self) -> str | None:
        """The scheme the *current* row was written with, so a UI can warn on rotation."""

        row = self._row()
        if row is None:
            return None
        try:
            scheme, _, _, _ = parse_hash(row.password_hash)
        except ValueError:
            return "unknown"
        return scheme

    def delete(self) -> None:
        """Forget the credential entirely, which is what a factory reset does.

        Present because the installer needs it (milestone 15) and because "delete the
        admin account" has to be a deliberate act with a name, not a `DELETE` someone
        adds to a migration.
        """

        if (row := self._row()) is not None:
            self._session.delete(row)
            self._session.flush()


def generate_password(*, bytes_of_entropy: int = 16) -> str:
    """A URL-safe secret, for the installer's first-boot output.

    Not a method of the repository on purpose: nothing about generating a credential is
    storage, and a repository that could mint passwords is a repository someone calls by
    accident.
    """

    return secrets.token_urlsafe(bytes_of_entropy)


def random_salt() -> bytes:
    """Entropy for a session key, from the same source the hasher uses.

    `os.urandom` rather than `random`: the module is a deterministic PRNG, and an
    appliance whose session cookies come from a seedable generator is a appliance whose
    sessions are predictable from the boot time.
    """

    return os.urandom(32)


def current_scheme_is_current(hasher: Pbkdf2Hasher, stored: str) -> bool:
    """Whether a stored hash used today's parameters.

    The admin page shows this. A hash written by an older build verifies forever, which
    is the right behaviour for login and the wrong one for a machine that should be
    quietly getting stronger — so the difference has to be visible somewhere.
    """

    try:
        scheme, parameters, _, _ = parse_hash(stored)
    except ValueError:
        return False
    return scheme == hasher.scheme() and int(parameters) == hasher.rounds


def select_one(session: Session) -> AdminState | None:
    """The row, or None. Kept separate so `is_configured` and `verify` read the same way."""

    return session.scalars(select(AdminState).where(AdminState.id == 1)).first()
