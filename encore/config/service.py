"""Loading and validating installation-time configuration (SAPRS 12.1-12.7).

`ConfigurationService` owns exactly one capability: turning the YAML an operator
installed into a validated `EncoreConfig`, once, at startup (SAPRS 11.7 step 1).

Three properties are the design:

* **Read-only.** There is no save method, because SAPRS 12.3 forbids Encore
  editing its own configuration, and a module cannot be blamed for a write it has
  no way to perform. Nothing here calls `open()` for writing, and
  `tests/unit/test_architecture_guardrails.py` checks that.
* **Fail-fast.** Invalid configuration raises `ConfigurationError` naming every
  problem at once (SAPRS 12.5). A jukebox that starts and then misbehaves is a
  worse party than one that refuses to start and says why.
* **Not a database.** Values are read and validated; state goes to `runtime.db`
  (SAPRS 12.4, 12.7).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from pydantic import ValidationError

from encore.config.models import EncoreConfig

__all__ = ["ConfigProblem", "ConfigurationError", "ConfigurationService"]

LOGGER = logging.getLogger("encore.config")


class ConfigProblem(NamedTuple):
    """One rejected setting: where it is, and what is wrong with it."""

    location: str
    message: str


class ConfigurationError(ValueError):
    """Configuration Encore cannot run with, stated actionably (SAPRS 12.5).

    The message lists *every* problem rather than the first, because an operator
    editing a file by hand wants the whole list in one pass; a restart per typo
    at 11pm is the failure mode this class exists to avoid. `problems` carries the
    same information as data for the admin screen and tests.

    Attributes:
        problems: Each rejected setting, in the order the document names them.
        missing_file: The path that was not there, when that is the whole problem.
    """

    def __init__(
        self,
        message: str,
        *,
        problems: Sequence[ConfigProblem] = (),
        missing_file: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.problems: tuple[ConfigProblem, ...] = tuple(problems)
        self.missing_file = missing_file


class ConfigurationService:
    """Loads `config.yaml` once and hands out the validated result.

    Args:
        path: File to read. Absolute in production (`/etc/encore/config.yaml`,
            SAPRS 13.3); anywhere is fine in a test.
        config: A ready-made configuration, for callers that construct values
            directly. Supplying both `path` and `config` is a programming error.
    """

    def __init__(self, path: Path | None = None, *, config: EncoreConfig | None = None) -> None:
        if path is not None and config is not None:
            raise ValueError("supply a path or a config, not both")
        self._path = None if path is None else Path(path).expanduser()
        self._loaded = config

    @property
    def path(self) -> Path | None:
        """Where the configuration came from, or None if it was supplied."""

        return self._path

    @property
    def current(self) -> EncoreConfig | None:
        """The configuration already in force, without triggering a read.

        Returns:
            The loaded configuration, or None if nothing has been loaded yet.
            Use this to ask "has the appliance got settings?"; use `config` to
            ask "what are they?".
        """

        return self._loaded

    @property
    def config(self) -> EncoreConfig:
        """The validated configuration, loaded on first use.

        Once loaded this is a stable object: every reader in the process sees the
        same settings, which is what "configuration is read-only at runtime"
        (SAPRS 12.4) has to mean for something twelve subsystems read.

        Raises:
            ConfigurationError: The file is missing, unreadable, not a YAML
                mapping, or fails validation.
        """

        if self._loaded is None:
            return self.load()
        return self._loaded

    def load(self) -> EncoreConfig:
        """Read, validate and cache the configuration file.

        Returns:
            The validated configuration, also available from `config`.

        Raises:
            ConfigurationError: Validation failed. The cache is left untouched,
                so a process that already has settings keeps running on them.
        """

        loaded = self._read()
        self._loaded = loaded
        return loaded

    def summary(self) -> dict[str, Any]:
        """Read-only view for the administrative interface (SAPRS 12.3).

        Section-ordered and JSON-serializable so a template can render it
        without knowing the model. It is `EncoreConfig` dumped, not copied:
        there are no secrets to strip, because the models refuse to hold any
        (SAPRS 12.6), and inventing a redaction step would imply otherwise.
        """

        return self.config.model_dump(mode="json")

    def _read(self) -> EncoreConfig:
        if self._path is None:
            return EncoreConfig()

        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise ConfigurationError(
                f"configuration file not found: {self._path} "
                "(copy examples/config.yaml to the installation path, SAPRS 13.2)",
                missing_file=self._path,
            ) from error
        except IsADirectoryError as error:
            raise ConfigurationError(f"configuration path {self._path} is not a file") from error
        except OSError as error:
            raise ConfigurationError(
                f"configuration file {self._path} cannot be read: {error}"
            ) from error

        try:
            raw: Any = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise ConfigurationError(f"{self._path} is not valid YAML: {error}") from error

        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigurationError(
                f"{self._path} must contain a mapping of sections, got {type(raw).__name__}"
            )

        # A section left empty (`audio:` with nothing under it) is an operator who
        # deleted the lines they did not want to change. That means "use the
        # defaults for audio", which is exactly what omitting the section means, so
        # the two are given the same shape here rather than one of them becoming a
        # validation error. Unknown keys inside a section still fail: `extra=forbid`
        # is not relaxed by this.
        raw = {key: ({} if value is None else value) for key, value in raw.items()}

        try:
            config = EncoreConfig.model_validate(raw)
        except ValidationError as error:
            raise ConfigurationError(
                _message_for(self._path, error), problems=_problems(error)
            ) from error

        LOGGER.info(
            "configuration loaded",
            extra={"path": str(self._path), "sections": sorted(raw)},
        )
        return config


def _problems(error: ValidationError) -> tuple[ConfigProblem, ...]:
    """Flatten pydantic's errors into (location, message) pairs.

    The location is joined with dots so it reads like the file it came from:
    `audio.crossfade_seconds`, not a tuple a template would have to unpack.
    """

    return tuple(
        ConfigProblem(
            location=".".join(str(part) for part in problem["loc"]) or "<root>",
            message=problem["msg"],
        )
        for problem in error.errors()
    )


def _message_for(path: Path, error: ValidationError) -> str:
    """The startup diagnostic SAPRS 12.5 asks for: every problem, named, at once."""

    problems = _problems(error)
    listed = "; ".join(f"{problem.location}: {problem.message}" for problem in problems)
    plural = "" if len(problems) == 1 else "s"
    return f"{path} has {len(problems)} configuration problem{plural} - {listed}"
