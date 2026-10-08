"""Structured logging: what the journal receives (SAPRS 5.8, 12.6, AEP 15-16).

SAPRS 5.8 requires structured logging; AEP 16 requires the level names and
forbids logging secrets. Both are observable in the emitted line, so every test
here captures real output from a real handler instead of asserting that some
internal function was called.

`configure()` installs on the root logger, so each test takes the fixture that
removes it again; leaving it installed would make every later test in the process
write into this one's sink.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from encore.config.models import LoggingConfig
from encore.services.logging_service import (
    ConsoleFormatter,
    JsonFormatter,
    LoggingService,
    record_fields,
)
from encore.utilities.redaction import REDACTED, is_sensitive, redact

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The keys every line must carry. A parser in the admin log view (milestone 14)
#: relies on them, and `logging_service.ENVELOPE` is the same tuple - restated
#: here so a change to the production constant has to be a deliberate one.
EXPECTED_ENVELOPE = ("timestamp", "level", "logger", "message")

#: The credential these tests prove never reaches the journal. Fictional, and
#: declared once so `detect-secrets` has one line to be told about rather than
#: five, and so a test that forgot to redact fails against the same string.
FAKE_PASSWORD = "hunter2"  # pragma: allowlist secret


@pytest.fixture
def sink() -> Iterator[io.StringIO]:
    """A logging service wired to a buffer, undone afterwards.

    Yields:
        The buffer; read it with `lines()`.
    """

    buffer = io.StringIO()
    service = LoggingService(LoggingConfig(format="json"), stream=buffer)
    service.configure()
    try:
        yield buffer
    finally:
        service.remove()


def lines(captured: io.StringIO) -> list[dict[str, object]]:
    """Every JSON object written so far, oldest first."""

    return [json.loads(line) for line in captured.getvalue().splitlines() if line.strip()]


# -- the JSON envelope ---------------------------------------------------


def test_a_line_is_one_json_object_carrying_the_envelope(sink: io.StringIO) -> None:
    logging.getLogger("encore.queue").info("queue advanced", extra={"position": 2})

    [record] = lines(sink)
    assert set(record) >= set(EXPECTED_ENVELOPE)
    assert record["level"] == "INFO"
    assert record["logger"] == "encore.queue"
    assert record["message"] == "queue advanced"
    assert record["position"] == 2


def test_a_record_with_no_extras_is_still_one_valid_object(sink: io.StringIO) -> None:
    logging.getLogger("encore").info("server started on :8080")

    assert len(lines(sink)) == 1


def test_the_timestamp_is_utc_and_precise_enough_to_order_a_second(
    sink: io.StringIO,
) -> None:
    """SAPRS 5.8's timestamp, with microseconds: three events inside one second
    must still come out of the journal in the order they went in."""

    logger = logging.getLogger("encore.timing")
    for index in range(3):
        logger.info("event %d", index)

    stamps = [str(record["timestamp"]) for record in lines(sink)]
    parsed = [datetime.fromisoformat(stamp) for stamp in stamps]
    assert parsed == sorted(parsed)
    assert all(moment.tzinfo is not None for moment in parsed)
    assert abs((datetime.now(UTC) - parsed[0]).total_seconds()) < 5


def test_an_extra_colliding_with_a_stdlib_attribute_is_refused_by_the_logger(
    sink: io.StringIO,
) -> None:
    """`extra={"levelname": ...}` never reaches a formatter, and that is stdlib's
    doing. Pinned because it is one of the reasons `logging` is sufficient here
    (AEP 10), and because the refusal must not disable the logger afterwards."""

    logger = logging.getLogger("encore.clash")

    with pytest.raises(KeyError):
        logger.info("attempts to redefine an attribute", extra={"levelname": "NOTHING"})

    logger.info("and then logs normally")

    [record] = lines(sink)
    assert record["level"] == "INFO"
    assert record["message"] == "and then logs normally"


def test_the_envelope_survives_a_record_that_carries_a_colliding_field() -> None:
    """Four keys are guaranteed, so a fifth with one of those names is renamed.

    The names in `ENVELOPE` are this module's, not stdlib's reserved set, so a
    record can legitimately arrive carrying one - from a third-party logging
    module, or a formatter wrapped around another. The line must still say what
    level it is, and the value must still be there under another name.
    """

    record = logging.LogRecord("encore.envelope", logging.INFO, "f.py", 1, "kept", (), None)
    record.level = "NOTHING"
    record.logger = "somewhere.else"
    record.message = "overwritten"

    payload = record_fields(record)

    assert payload["message"] == "kept", "the envelope is authoritative"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "encore.envelope"
    assert payload["extra_level"] == "NOTHING"
    assert payload["extra_logger"] == "somewhere.else"
    assert "extra_message" not in payload, (
        "`message` is one of stdlib's own attributes, so a record cannot carry a "
        "colliding extra for it; the prefix only appears where stdlib leaves a gap"
    )


def test_interpolation_happens_in_the_record_not_in_the_line(sink: io.StringIO) -> None:
    logger = logging.getLogger("encore.fmt")
    logger.warning("%s queued %d", "Nobody", 3)

    [record] = lines(sink)
    assert record["message"] == "Nobody queued 3"


def test_a_malformed_format_string_still_produces_a_line() -> None:
    """A broken `%` is a bug in the calling service, and it surfaces at the worst
    moment - usually inside an exception handler. stdlib's answer is to print to
    stderr and discard the record; for an unattended appliance that trades a
    cosmetic bug for a lost event, so the pieces are joined instead."""

    logger = logging.getLogger("encore.fmt")
    record = logger.makeRecord(
        "encore.fmt", logging.ERROR, "f.py", 1, "count is %d", ("not-a-number",), None
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "count is %d", "the template is what the service wrote"
    assert payload["unformatted_args"] == ["not-a-number"], "and the values are still there"
    assert payload["level"] == "ERROR"


def test_exception_details_travel_with_the_record(sink: io.StringIO) -> None:
    """SAPRS 5.8 wants enough context to diagnose; a traceback is that context."""

    try:
        (_ for _ in ()).throw(FileNotFoundError("library.db"))
    except FileNotFoundError:
        logging.getLogger("encore.library").exception("library load failed")

    [record] = lines(sink)
    assert record["level"] == "ERROR"
    traceback = str(record["exception"])
    assert traceback.startswith("Traceback")
    assert "FileNotFoundError: library.db" in traceback


def test_unserializable_extras_are_stringified_rather_than_dropped(
    sink: io.StringIO,
) -> None:
    """A service will pass a Path or a datetime in `extra`; losing the field to a
    `TypeError` inside the handler would lose the log line too."""

    logging.getLogger("encore.types").info(
        "looked at a file",
        extra={"path": Path("/srv/music/a/b/c.mp3"), "when": datetime(2026, 5, 4, 21, 30)},
    )

    [record] = lines(sink)
    assert record["path"] == "/srv/music/a/b/c.mp3"
    assert str(record["when"]).startswith("2026-05-04T21:30"), "ISO, so it round-trips"


# -- redaction (SAPRS 12.6, AEP 16-17) ----------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("password", "correct-horse"),
        ("admin_password_hash", "$2b$12$abcdefghijklmnop"),
        ("session_secret", "deadbeef"),
        ("api_key", "sk-1234567890"),
        ("token", "eyJhbGciOiJIUzI1NiJ9"),
        ("authorization", "Bearer eyJhbGci"),
        ("cookie", "session_id=abcdef"),
        ("mpv_password", FAKE_PASSWORD),
    ],
)
def test_a_credential_field_is_withheld(sink: io.StringIO, field: str, value: str) -> None:
    """Checked against the rendered line, not the record: a formatter that redacts
    the value and then prints it from `args` would pass a weaker test."""

    logging.getLogger("encore.admin").info("login attempted", extra={field: value})

    dumped = json.dumps(lines(sink))
    assert value not in dumped, f"{field} reached the journal in the clear"
    assert REDACTED in dumped, "the field must still be visible as deliberately withheld"


@pytest.mark.parametrize(
    ("field", "value", "withheld"),
    [
        ("song_id", 17, False),
        ("file_path", "/srv/music/a.mp3", False),
        ("playback_position", 12.5, False),
        ("session_minutes", 480, True),
        ("pin_code", "1234", True),
        ("refresh_token", "abcdef", True),
    ],
)
def test_the_list_of_sensitive_names_is_broad_but_not_blind(
    field: str, value: object, withheld: bool
) -> None:
    """The costs are asymmetric, so the list leans wide: an unreadable
    `session_minutes` is one confusing field, a printed `refresh_token` is a
    published credential. `session_minutes` is withheld for that reason even
    though it is only a number - a name containing "session" is treated as
    belonging to the session, and a service that wants the number in the journal
    should call the field `session_lifetime_minutes`."""

    assert is_sensitive(field) is withheld
    assert redact(field, value) == (REDACTED if withheld else value)


def test_redaction_is_the_formatter_s_job_not_the_caller_s(sink: io.StringIO) -> None:
    """A service forgetting to redact is the normal case: the check has to sit at
    the one place every line passes through."""

    def helper() -> None:
        logging.getLogger("encore.admin").info(
            "attempt",
            extra={"password": FAKE_PASSWORD},
        )

    helper()

    assert FAKE_PASSWORD not in json.dumps(lines(sink))


def test_record_fields_is_the_single_answer_to_what_was_logged() -> None:
    """Both formatters and the tests read fields through one function."""

    record = logging.LogRecord("encore.x", logging.INFO, "f.py", 1, "hi %s", ("there",), None)
    record.song_id = 9
    record.password = FAKE_PASSWORD

    fields = record_fields(record)
    assert fields["message"] == "hi there"
    assert fields["song_id"] == 9
    assert fields["password"] == REDACTED


# -- the console format -------------------------------------------------


def test_the_console_format_carries_the_same_fields() -> None:
    """`logging.format: console` is for a terminal, not for a weaker policy."""

    formatter = ConsoleFormatter()
    record = logging.LogRecord(
        "encore.playback", logging.WARNING, "f.py", 2, "restarting", (), None
    )
    record.restart_count = 2
    record.password = FAKE_PASSWORD

    line = formatter.format(record)
    assert "WARNING" in line
    assert "encore.playback" in line
    assert "restarting" in line
    assert "restart_count=2" in line
    assert FAKE_PASSWORD not in line


def test_the_format_choice_comes_from_configuration() -> None:
    buffer = io.StringIO()
    service = LoggingService(LoggingConfig(format="console"), stream=buffer)
    service.configure()
    try:
        logging.getLogger("encore").info("console line")
    finally:
        service.remove()

    text = buffer.getvalue()
    assert text.strip().endswith("console line") or "console line" in text
    assert not text.lstrip().startswith("{"), "console format must not emit JSON"


def test_the_json_default_is_what_the_shipped_example_selects() -> None:
    assert LoggingConfig().format == "json"


# -- levels and lifecycle (SAPRS 12.2, AEP 16) -------------------------


Levels = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_the_four_documented_levels_all_work(level: Levels) -> None:
    """AEP 16 names four levels; an operator can pick any of them in the file."""

    buffer = io.StringIO()
    service = LoggingService(LoggingConfig(level=level), stream=buffer)
    service.configure()
    logger = logging.getLogger("encore.levels")
    try:
        for candidate in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            getattr(logger, candidate.lower())("probing at %s", candidate)
    finally:
        service.remove()

    expected = {
        candidate
        for candidate in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
        if logging.getLevelNamesMapping()[candidate] >= logging.getLevelNamesMapping()[level]
    }
    assert {str(record["level"]) for record in lines(buffer)} == expected


def test_the_configured_level_is_reported(sink: io.StringIO) -> None:
    service = LoggingService(LoggingConfig(level="WARNING"), stream=sink)

    assert service.level == logging.WARNING


def test_configure_is_idempotent_for_one_service(sink: io.StringIO) -> None:
    """Configuring twice must not attach the handler twice and make the journal
    report every event twice."""

    buffer = io.StringIO()
    service = LoggingService(LoggingConfig(format="json"), stream=buffer)
    service.configure()
    service.configure()
    try:
        logging.getLogger("encore.once").info("written once")
        assert len(lines(buffer)) == 1
    finally:
        service.remove()


def test_removing_leaves_the_process_as_it_was_found(sink: io.StringIO) -> None:
    """AEP 9's reasoning about owned state: what a service attaches, it detaches."""

    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level

    service = LoggingService(LoggingConfig(), stream=io.StringIO())
    service.configure()
    assert list(root.handlers) != before
    service.remove()

    assert list(root.handlers) == before
    assert root.level in (before_level, service.level)


def test_a_third_party_logger_keeps_its_own_handlers(sink: io.StringIO) -> None:
    """Encore configures the root; it must not reach into uvicorn's plumbing."""

    handler = logging.NullHandler()
    uvicorn = logging.getLogger("uvicorn")
    uvicorn.addHandler(handler)
    service = LoggingService(LoggingConfig(), stream=io.StringIO())
    service.configure()
    try:
        assert handler in uvicorn.handlers
    finally:
        service.remove()
        uvicorn.removeHandler(handler)


def test_get_logger_returns_the_stdlib_logger_for_a_name(sink: io.StringIO) -> None:
    service = LoggingService(LoggingConfig(), stream=sink)

    assert service.get_logger("encore.search") is logging.getLogger("encore.search")


def test_no_application_module_prints_to_a_terminal() -> None:
    """AEP 16 forbids `print` in application code; ruff's T20 rule enforces it in
    CI, and this keeps a stray call out of the package between runs."""

    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "encore").rglob("*.py")
        if "print(" in path.read_text(encoding="utf-8")
        and not path.name.startswith("test_")
        and "utilities" not in path.parts
    ]
    assert offenders == []


def test_the_logging_service_does_not_read_the_environment() -> None:
    """SAPRS 12.4 allows one input to configuration: the YAML file."""

    source = (REPO_ROOT / "encore" / "services" / "logging_service.py").read_text(encoding="utf-8")
    assert "environ" not in source
    assert "getenv" not in source


# -- the two formatter branches that only a mistake reaches -------------


def test_keyword_interpolation_is_reported_as_the_values_it_used() -> None:
    """`%(name)s` styles put the values in a mapping, not a tuple.

    Reached only when the mapping is missing a key the template names - which is
    precisely when a line must still be written, because the missing key usually
    *is* the diagnosis."""

    logger = logging.getLogger("encore.kwargs")
    record = logger.makeRecord(
        "encore.kwargs", logging.ERROR, "f.py", 1, "song %(title)s by %(artist)s", (), None
    )
    record.args = {"title": "Bela"}  # 'artist' missing: raises KeyError in getMessage

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "song %(title)s by %(artist)s"
    assert payload["unformatted_args"] == ["title=Bela"]


def test_a_scalar_arg_survives_a_format_string_that_cannot_use_it() -> None:
    """`msg % args` fails when the message has no placeholder for the value - a
    message written one way and called another. The value is still evidence, so it
    is reported rather than dropped along with the line."""

    record = logging.LogRecord("encore.scalar", logging.ERROR, "f.py", 1, "no template", (7,), None)

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "no template"
    assert payload["unformatted_args"] == ["7"], "the value is still the evidence"


def test_stack_info_is_recorded_separately_from_the_exception(sink: io.StringIO) -> None:
    """`exc_info` says where the error was raised; `stack_info` says how the code
    got there. Folding one into the other loses a traceback an operator needs."""

    logging.getLogger("encore.stack").error("stuck", stack_info=True)

    [record] = lines(sink)
    assert "Stack (most recent call last)" in str(record["stack"])
    assert "exception" not in record
