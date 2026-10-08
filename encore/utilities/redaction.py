"""Sensitive field names, and the one function that hides them (AEP 16, AEP 17).

`logging` cannot know that a value is a credential, but it is the layer that
would print one. This module exists so the answer to "what must never appear in
a log line?" is written once, next to nothing else, and shared by the log
formatter and the configuration summary.

Keep the list short and case-insensitive. It is a backstop for a mistake, not a
substitute for keeping secrets out of the process (SAPRS 12.6), and a value that
reaches this function has already been judged safe to log by its owner.
"""

from __future__ import annotations

from typing import Final

__all__ = ["REDACTED", "SENSITIVE_FIELDS", "SENSITIVE_WORDS", "is_sensitive", "redact"]

#: What a sensitive value is replaced with. Valid JSON, obviously not a secret.
REDACTED: Final[str] = "[redacted]"

#: Substrings that make a field name sensitive. Deliberately broad: the cost of
#: over-redacting a `token_count` is one unreadable field, and the cost of
#: under-redacting a `refresh_token` is a published credential.
SENSITIVE_FIELDS: Final[tuple[str, ...]] = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "pin",
    "secret",
    "session",
    "token",
)

#: Whole words that make a name sensitive when they stand alone as a component.
#: "key" is here because `api_key` and `access_key` are the common forms, and it
#: is not a substring marker because that would catch `monkey` and `keyboard`.
SENSITIVE_WORDS: Final[frozenset[str]] = frozenset({"key", "password", "secret", "token"})


def is_sensitive(field: str) -> bool:
    """True when `field`'s name means its value must not be logged.

    A name ending in a bare `key` counts, which does mean `sort_key` is withheld.
    That is the accepted cost: artist sort keys are metadata anyone can read in the
    library, and a missed `api_key` is not.
    """

    lowered = field.lower()
    if any(marker in lowered for marker in SENSITIVE_FIELDS):
        return True
    return any(word in lowered.split("_") for word in SENSITIVE_WORDS)


def redact(field: str, value: object) -> object:
    """Return `value`, or `REDACTED` when the field name says it is sensitive."""

    return REDACTED if is_sensitive(field) else value
