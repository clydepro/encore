"""Validated configuration shape (SAPRS Chapter 12, AIG 4).

One model per section of `examples/config.yaml`, which is the document an
operator installs from; these classes are that document made executable. If the
two ever disagree, the model is right and the example is a defect - the example
is loaded and validated by `tests/unit/test_configuration.py` on every commit, so
disagreement fails CI rather than someone's first boot.

Two rules from SAPRS Chapter 12 are enforced by construction rather than by
documentation:

* **`extra="forbid"` everywhere.** A misspelled key is not silently ignored; it
  stops startup with the offending name (SAPRS 12.5). The same rule means a key
  holding a secret cannot be added by accident, because no model here declares
  one (SAPRS 12.6).
* **`frozen=True` everywhere.** Configuration is not application-managed state
  (SAPRS 12.3), and an immutable model cannot become a place to stash it.

Defaults are the appliance values from SAPRS 13.3 and `examples/config.yaml`, so
an empty file is a valid minimal configuration rather than an error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "AdminConfig",
    "AudioConfig",
    "EncoreConfig",
    "LoggingConfig",
    "PathsConfig",
    "PlaybackConfig",
    "QueueConfig",
    "SearchConfig",
    "ServerConfig",
]

#: Named queue ceiling Encore accepts. SAPRS 8.4 wants roughly 25-50 items and
#: says any maximum must be explicit and visible; 200 leaves headroom for the
#: 100-guest stress profile without letting the queue grow without bound.
MAX_QUEUE_ITEMS_LIMIT: Final[int] = 10_000


class _Section(BaseModel):
    """Common rules for every section: no surprises, no mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ServerConfig(_Section):
    """Network/server settings (SAPRS 1.4, 12.2).

    Attributes:
        host: Bind address. Default is every interface, which is correct for a
            LAN appliance and wrong for a machine that also faces the internet;
            `docs/Administrator-Guide.md` says so in the security section.
        port: TCP port for the guest and admin interfaces.
        hostname: mDNS name advertised to guests.
        domain: Fully qualified local name guests type or scan.
    """

    host: str = "0.0.0.0"  # noqa: S104 - LAN appliance by design, see SAPRS 1.4
    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    hostname: str = "jukebox"
    domain: str = "jukebox.home.arpa"

    @field_validator("host", "hostname", "domain")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class PathsConfig(_Section):
    """Where the appliance keeps things (SAPRS 12.2, 13.3).

    Paths are absolute because systemd units run with an undefined working
    directory, and a relative path that works when a developer starts the server
    by hand and breaks under `systemctl start encore` is the worse failure.
    """

    library_db: Path = Path("/var/lib/encore/library.db")
    runtime_db: Path = Path("/var/lib/encore/runtime.db")
    artwork_dir: Path = Path("/var/lib/encore/artwork")
    temp_dir: Path = Path("/var/cache/encore/temp")

    @model_validator(mode="after")
    def _databases_are_distinct(self) -> PathsConfig:
        if self.library_db == self.runtime_db:
            raise ValueError(
                "library_db and runtime_db must be different files: the runtime opens "
                "library.db read-only and writes runtime.db (SAPRS 5.1, ADR-006)"
            )
        return self

    @field_validator("library_db", "runtime_db", "artwork_dir", "temp_dir")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError(f"must be absolute (SAPRS 13.3), got {value}")
        return value


class AudioConfig(_Section):
    """Audio output (SAPRS 7.7, 7.8, 12.2).

    Hardware specifics live here so no playback module ever embeds them.
    """

    output: str = "alsa"
    device: str = "default"
    volume: Annotated[int, Field(ge=0, le=100)] = 85
    gapless: bool = True
    crossfade_seconds: Annotated[float, Field(ge=0, le=12)] = 0.0

    @field_validator("output", "device")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class QueueConfig(_Section):
    """Queue limits (SAPRS 8.4, 12.2).

    `allow_duplicates` exists to be observed, not to be changed: SAPRS 8.3
    prohibits queue deduplication outright, so a configuration that turns
    duplicates off would be an unsupported product, not a preference. It is
    therefore validated to `True`, and the field is present so an operator who
    reads SAPRS 8.3 as a knob gets a clear refusal instead of silence.
    """

    max_items: Annotated[int, Field(ge=1, le=MAX_QUEUE_ITEMS_LIMIT)] = 200
    allow_duplicates: bool = True

    @model_validator(mode="after")
    def _duplicates_stay_allowed(self) -> QueueConfig:
        if not self.allow_duplicates:
            raise ValueError(
                "allow_duplicates cannot be false: SAPRS 8.3 prohibits queue "
                "deduplication, so a build that removed duplicate requests would not be "
                "Encore"
            )
        return self


class PlaybackConfig(_Section):
    """The player and its recovery (SAPRS 7.2, 7.5, 7.6).

    Attributes:
        mpv_path: Executable the Supervisor launches.
        restart_backoff_seconds: Delay before each successive restart, so a box
            that cannot start mpv waits instead of spinning (SAPRS 7.6). The
            last value is reused once the list is exhausted.
        progress_interval_seconds: How often progress is reported, roughly the
            once-per-second SAPRS 7.5 asks for.
    """

    mpv_path: Path = Path("/usr/bin/mpv")
    restart_backoff_seconds: Annotated[list[float], Field(min_length=1)] = Field(
        default_factory=lambda: [1.0, 2.0, 5.0, 10.0, 30.0]
    )
    progress_interval_seconds: Annotated[float, Field(gt=0, le=10)] = 1.0

    @model_validator(mode="after")
    def _backoff_is_positive(self) -> PlaybackConfig:
        for value in self.restart_backoff_seconds:
            if value <= 0:
                raise ValueError(f"restart backoff must be positive seconds, got {value}")
        return self


class SearchConfig(_Section):
    """Search presentation limits (SAPRS 11, 12.2)."""

    page_size: Annotated[int, Field(ge=1, le=500)] = 50


class LoggingConfig(_Section):
    """Logging (SAPRS 12.2, AEP 16).

    Attributes:
        level: Minimum level emitted.
        format: `json` for the journal, `console` for a terminal.
    """

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "console"] = "json"


class AdminConfig(_Section):
    """Administrative session lifetime (SAPRS 12.6, Chapter 14 of AIG).

    Deliberately contains no credential: session secrets come from the
    environment or the system store, and `extra="forbid"` means a
    `password:` or `secret:` key added to this file is a startup error rather
    than a false sense of security (SAPRS 12.6, AEP 17).
    """

    session_minutes: Annotated[int, Field(ge=1, le=7 * 24 * 60)] = 60


class EncoreConfig(_Section):
    """The whole validated configuration (SAPRS 12.2).

    Sections may be omitted from the YAML file and take their documented
    defaults; unknown keys, in a section or at the top level, never do.
    """

    # Nested models are revalidated even when an instance of the right class is
    # handed in. `model_copy` skips validation, so without this a queue section
    # edited in memory could arrive here with a rule from 8.3 switched off.
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    server: ServerConfig = Field(default_factory=ServerConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    playback: PlaybackConfig = Field(default_factory=PlaybackConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
