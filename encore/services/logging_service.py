"""Structured logging (AEP 16, AIG 17).

Encore logs for one reader: the person deciding whether the box is broken or the
WiFi is. That constrains the design more than any preference does.

* **JSON by default.** systemd captures the journal, and the admin log view
  (milestone 14) filters on fields. Prose has to be grepped.
* **Stdlib only.** `logging` does this. AEP 10's first question - "is the standard
  library sufficient?" - has no better answer here than to take it.
* **Nothing is logged that was not handed over**, and the field names that must
  never be printed are listed once, in `encore/utilities/redaction.py`, and
  applied by the formatter itself rather than trusted to callers (AEP 16, 17).

`configure()` is idempotent and removes only its own handler, so a test using
`caplog`, or a library that installed a sink of its own, keeps what it added.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Final, TextIO, cast

from encore.config.models import LoggingConfig
from encore.utilities.redaction import redact

__all__ = ["ENVELOPE", "ConsoleFormatter", "JsonFormatter", "LoggingService", "record_fields"]

#: Attributes `logging` sets on every record. Anything else in `record.__dict__`
#: arrived through `extra=` and is the reason the line exists.
_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

#: The four keys every line carries, and so the four an `extra` must not replace.
#: Data rather than a literal repeated in the formatters and in the tests.
ENVELOPE: Final[tuple[str, ...]] = ("timestamp", "level", "logger", "message")


def record_fields(record: logging.LogRecord) -> dict[str, object]:
    """The fields one record carries: the standard four, plus whatever was sent
    in `extra`, with sensitive names replaced.

    Shared by both formatters, and by tests and the admin log view, so that
    "what did we record?" has one answer instead of three that can drift.

    The envelope is protected. stdlib `logging` already refuses an `extra` key that
    collides with its own attributes, but not with the four names this function
    publishes, so `extra={"message": ...}` would otherwise replace the sentence the
    line exists to carry. Collisions are kept under an `extra_` prefix rather than
    dropped: the value is somebody's diagnostic, and losing it silently is its own
    bug.

    A malformed `%` in a message is a bug in the calling service, and it surfaces at
    the worst moment - usually inside an exception handler. stdlib's answer is to
    print to stderr and discard the record; for an unattended appliance that trades
    a cosmetic bug for a lost event, so the template is kept and the values are
    reported beside it under `unformatted_args`.
    """

    payload: dict[str, object] = {
        "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="microseconds"),
        "level": record.levelname,
        "logger": record.name,
        "message": str(record.msg),
    }
    if record.args:
        try:
            payload["message"] = record.getMessage()
        except (TypeError, ValueError, KeyError):
            payload["unformatted_args"] = _args(record.args)
    for key, value in vars(record).items():
        if key in _RESERVED:
            continue
        payload[key if key not in ENVELOPE else f"extra_{key}"] = redact(key, value)
    return payload


def _args(args: object) -> list[str]:
    """The interpolation values of a record whose format string did not resolve.

    `LogRecord.args` is either a positional tuple or, after stdlib's own
    unwrapping of a single mapping argument, that mapping. Both are reported as
    strings: the values are the evidence, in either shape.
    """

    if isinstance(args, Mapping):
        return [f"{name}={value}" for name, value in args.items()]
    # The cast states stdlib's guarantee instead of re-checking it: `LogRecord`
    # rejects a non-iterable `args` in its own constructor, so whatever reaches
    # here is positional.
    positional = cast(Iterable[object], args)
    return [str(argument) for argument in positional]


def _encode(value: object) -> str:
    """JSON for the types services actually log: paths, datetimes, anything else.

    Plain `str` renders a datetime as "2026-05-04 21:30:00", which no parser reads
    back; `isoformat` keeps the ones that matter round-trippable and still turns
    everything else into a string, so an unexpected type costs a field, not a line.
    """

    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = record_fields(record)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=_encode, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """Single-line text for a terminal, carrying the same fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = record_fields(record)
        stamp = str(payload["timestamp"])[11:19]
        extras = " ".join(
            f"{key}={value}" for key, value in payload.items() if key not in set(ENVELOPE)
        )
        line = f"{stamp} {record.levelname:<7} {record.name}: {payload['message']}"
        return f"{line} {extras}" if extras else line


class LoggingService:
    """Configures process logging once, and hands out named loggers.

    Args:
        config: The validated `logging` section, passed in rather than read from
            a global (SAPRS 11.5).
        stream: Where to write. Defaults to stderr, which is what systemd
            captures (SAPRS 13.6).
    """

    def __init__(self, config: LoggingConfig, *, stream: TextIO | None = None) -> None:
        self._config = config
        self._stream: TextIO = stream if stream is not None else sys.stderr
        self._handler: logging.Handler | None = None

    @property
    def level(self) -> int:
        """The numeric level Encore is running at."""

        return logging.getLevelNamesMapping()[self._config.level]

    def configure(self) -> LoggingService:
        """Install this service's handler on the root logger.

        Returns self so a composition root can build and configure in one
        expression with no module-level state to remember.
        """

        self.remove()
        handler = logging.StreamHandler(self._stream)
        handler.setLevel(self.level)
        handler.setFormatter(self._formatter())
        self._handler = handler
        root = logging.getLogger()
        root.addHandler(handler)
        # The root takes the configured level, so a dependency logging at INFO is
        # heard at INFO. That is the honest reading of SAPRS 12.2 ("level") and
        # the right default for an appliance whose whole log is one stream; if a
        # dependency ever becomes loud, the fix is a per-logger level in
        # `LoggingConfig`, not a lower root.
        root.setLevel(self.level)
        return self

    def remove(self) -> None:
        """Detach the handler this service installed. Never touches another's."""

        if self._handler is None:
            return
        logging.getLogger().removeHandler(self._handler)
        self._handler.close()
        self._handler = None

    def get_logger(self, name: str) -> logging.Logger:
        """A logger for one component, sharing this service's configuration."""

        return logging.getLogger(name)

    def _formatter(self) -> logging.Formatter:
        return JsonFormatter() if self._config.format == "json" else ConsoleFormatter()
