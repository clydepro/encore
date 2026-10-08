"""Party Simulation profile loader (SAPRS 14.12).

Parses `tests/party_simulation/profiles/*.toml` into typed values so the
simulation driver can stay declarative. Profiles are test data: they describe
what the simulated guests do, never how the appliance is configured.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILES_DIR: Path = Path(__file__).resolve().parents[1] / "party_simulation" / "profiles"

#: The SAPRS 14.11 reference party.
BASELINE_PROFILE = "baseline"


class InvalidProfileError(ValueError):
    """Raised when a profile file is missing keys or has the wrong types."""


@dataclass(frozen=True)
class PartyProfile:
    """A declarative party definition used by the simulation suite."""

    name: str
    description: str
    guests: int
    duration_minutes: int
    library_songs: int
    searches_per_guest_per_minute: int
    browsing_per_guest_per_minute: int
    queue_adds_per_guest_per_minute: int
    duplicates_allowed: bool
    admin_activity: bool
    mpv_terminations: int
    client_disconnects: bool
    sse_reconnects: bool

    @property
    def guest_minutes(self) -> int:
        """Total guest-minutes of attention the profile demands."""

        return self.guests * self.duration_minutes

    @property
    def queue_adds_per_minute(self) -> int:
        return self.guests * self.queue_adds_per_guest_per_minute


def load_profile(path: Path) -> PartyProfile:
    """Read one profile file into a `PartyProfile`."""

    try:
        with path.open("rb") as handle:
            raw: dict[str, Any] = tomllib.load(handle)
    except OSError as error:  # pragma: no cover - exercised via load failure below
        raise InvalidProfileError(f"cannot read profile {path}: {error}") from error

    profile = _section(raw, "profile")
    library = _section(raw, "library")
    activity = _section(raw, "activity")
    chaos = _section(raw, "chaos")

    return PartyProfile(
        name=_text(profile, "name"),
        description=_text(profile, "description"),
        guests=_integer(profile, "guests"),
        duration_minutes=_integer(profile, "duration_minutes"),
        library_songs=_integer(library, "songs"),
        searches_per_guest_per_minute=_integer(activity, "searches_per_guest_per_minute"),
        browsing_per_guest_per_minute=_integer(activity, "browsing_per_guest_per_minute"),
        queue_adds_per_guest_per_minute=_integer(activity, "queue_adds_per_guest_per_minute"),
        duplicates_allowed=_boolean(activity, "duplicates_allowed"),
        admin_activity=_boolean(activity, "admin_activity"),
        mpv_terminations=_integer(chaos, "mpv_terminations"),
        client_disconnects=_boolean(chaos, "client_disconnects"),
        sse_reconnects=_boolean(chaos, "sse_reconnects"),
    )


def available_profiles() -> list[Path]:
    return sorted(PROFILES_DIR.glob("*.toml"))


def default_profile() -> PartyProfile:
    """The profile used when `--party-profile` is not supplied."""

    path = Path(PROFILES_DIR / f"{BASELINE_PROFILE}.toml")
    return load_profile(path) if path.exists() else load_profile(PROFILES_DIR / "house_party.toml")


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    section = raw.get(key)
    if not isinstance(section, dict):
        raise InvalidProfileError(f"missing [{key}] section")
    return {str(name): value for name, value in section.items()}


def _text(section: dict[str, Any], key: str) -> str:
    value = section.get(key)
    if not isinstance(value, str):
        raise InvalidProfileError(f"expected string for {key!r}, got {value!r}")
    return value


def _integer(section: dict[str, Any], key: str) -> int:
    value = section.get(key)
    if not isinstance(value, int):
        raise InvalidProfileError(f"expected integer for {key!r}, got {value!r}")
    return value


def _boolean(section: dict[str, Any], key: str) -> bool:
    value = section.get(key)
    if not isinstance(value, bool):
        raise InvalidProfileError(f"expected boolean for {key!r}, got {value!r}")
    return value
