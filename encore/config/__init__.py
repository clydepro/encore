"""Installation-time configuration: loading, validation and read-only summary.

Implements the model described in SAPRS Chapter 12: a YAML file supplied at
installation time, validated at startup, and never used as managed state.

* Invalid configuration must prevent startup with a clear diagnostic.
* Secrets come from the environment or system store, never from source.

Public surface: `ConfigurationService`, `ConfigurationError`, `ConfigProblem`
(the machine-readable form of a rejected setting, for the admin screen) and the
section models. Import from here rather than from a module inside the package.
"""

from __future__ import annotations

from encore.config.models import (
    AdminConfig,
    AudioConfig,
    EncoreConfig,
    LoggingConfig,
    PathsConfig,
    PlaybackConfig,
    QueueConfig,
    SearchConfig,
    ServerConfig,
)
from encore.config.service import ConfigProblem, ConfigurationError, ConfigurationService

__all__ = [
    "AdminConfig",
    "AudioConfig",
    "ConfigProblem",
    "ConfigurationError",
    "ConfigurationService",
    "EncoreConfig",
    "LoggingConfig",
    "PathsConfig",
    "PlaybackConfig",
    "QueueConfig",
    "SearchConfig",
    "ServerConfig",
]
