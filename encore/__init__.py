"""Encore — headless music jukebox appliance.

This package is the runtime home of the Encore server: API controllers,
configuration, domain services, the event bus, playback, repositories, search
and shared utilities.

The bootstrap phase (PBK) intentionally creates the package skeleton only. Each
sub-package owns exactly one capability, per AIG Chapter 7 and SAPRS 11.6, and
is populated by the implementation milestones in AIG Chapter 21.

See ``docs/SAPRS/Encore-SAPRS.md`` for the architectural reference and
``docs/adr/`` for the decisions behind it.
"""

from __future__ import annotations

__version__ = "0.1.0"
"""Distribution version; kept in step with ``pyproject.toml``."""

__all__ = ["__version__"]
