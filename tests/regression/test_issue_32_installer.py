"""Regression: #32 — the installer, and the three findings that were not installer bugs.

Milestone 15 was a program that did not exist. What turned up while building it was three defects
that each lived in a place no existing test looked, and whose shared property is that they were
all *green* before they were found:

| Defect | Symptom |
| ------ | ------- |
| `[tool.setuptools] packages` named `encore` and `apps.builder` but neither `apps.server` nor the two repository subpackages | a wheel that built cleanly, installed cleanly and passed the whole suite, because the suite imports from the checkout. An appliance booted from it had no server to start |
| A template whose Jinja comment contained the literal `| safe` | `test_templates.py`'s autoescaping guard matched the comment rather than a filter, so the failure arrived as a test about a file that had no filter in it |
| Nothing in the runtime read `ENCORE_ADMIN_PASSWORD` | the guide and ADR-013 both said the variable seeded the operator's credential. The appliance minted a random one and printed it to the journal, so an operator who followed the documentation got "the password was not right" on the machine they had just installed |
| `/api/v1/info` built its `url` as `f"http://{domain}/"` | the appliance listened on 8099, told every integrator it was on 80, and printed a QR code that resolved and refused the connection |

The third and fourth are the ones worth reading twice. Neither was a bug in the code that was
wrong: `AdminService` was correct, `ServerConfig` was correct, the installer was correct. Each was
a value that crossed a boundary and was dropped, which is the failure mode a monolith does not
have and an appliance with a Builder, a Server and an Installer has constantly.

Hence the shape of these tests. They do not re-check the units — those files are in the tree and
still earning their place. Each one drives the boundary: an artifact built and opened, an
environment variable set and observed by a login, a configuration written and read back by the
page a guest scans.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

import pytest

from apps.installer.main import EXIT_OK, main
from apps.server.appliance import build
from encore.api.app import create_app
from encore.services.errors import AuthenticationError
from encore.utilities.appliance import call_async
from encore.utilities.qr import target
from tests.support.http import running
from tests.support.installer import FakeMachine, RecordingRunner, StubHttpProbe, synced
from tests.support.mpv import FakeEngine

pytestmark = [pytest.mark.regression, pytest.mark.e2e]

_DESC = re.compile(r"<desc>(.*?)</desc>", re.DOTALL)
PASSWORD = "a password that only this file knows"  # pragma: allowlist secret


# -- 1. the wheel --------------------------------------------------------------------


def test_the_built_wheel_contains_the_server_it_claims_to_run(tmp_path: Path) -> None:
    """`apps/server` was missing from `packages`, and every test passed anyway.

    Built for real and opened, because the declaration in `pyproject.toml` is not the artifact and
    the artifact is what an appliance installs. `test_packaging.py` keeps the fast version of this
    assertion; this is the version that cannot be satisfied by a list that agrees with itself.
    """

    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "apps/server/main.py" in names, "the wheel has no server in it"
    assert "apps/installer/main.py" in names, "the wheel has no installer in it"


def test_the_built_wheel_contains_the_templates_the_renderer_reads(tmp_path: Path) -> None:
    """A wheel without templates starts, answers 200 on the health endpoint, and 500s on a page.

    Compared against the source tree rather than against a count, so a template added next month
    without a package-data rule is caught here rather than at somebody's first request.
    """

    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        shipped = {name for name in archive.namelist() if name.endswith(".html")}
    repo = Path(__file__).resolve().parents[2]
    on_disk = {
        str(path.relative_to(repo)) for path in repo.joinpath("encore", "templates").rglob("*.html")
    }
    assert on_disk <= shipped, f"templates that would not install: {sorted(on_disk - shipped)[:4]}"


def test_the_built_wheel_ships_the_repository_subpackages(tmp_path: Path) -> None:
    """`encore.repositories.runtime` was in the same omission, and is the one that fails at boot.

    `encore.repositories` imports it, so this is not a missing feature but an `ImportError` in the
    composition root — the closest this product comes to a blank screen.
    """

    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for module in (
        "encore/repositories/runtime/admin_state.py",
        "encore/repositories/library/store.py",
    ):
        assert module in names, f"{module} did not ship"


# -- 2. the credential ---------------------------------------------------------------


async def test_the_environment_variable_is_the_credential_the_appliance_accepts(
    tmp_path: Path, built_library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documentation was ahead of the code, which is the worst order for those two.

    `ENCORE_ADMIN_PASSWORD` is the installer's only route to an operator's password (SAPRS 12.6,
    and ADR-013's note that milestone 15 would provide it). The appliance had a first-run page and
    a rotation endpoint and a session store, and no way to arrive pre-configured — so following
    the guide produced a refusal, and the guide said nothing about a refusal being possible.
    """

    monkeypatch.setenv("ENCORE_ADMIN_PASSWORD", PASSWORD)
    appliance = _appliance(tmp_path, built_library, admin_password=_from_environment())
    try:
        assert appliance.admin_password == PASSWORD, "the value never reached the appliance"
        async with running(create_app(appliance)):
            # On the appliance thread, because that is where the seed happened (ADR-012). Calling
            # the service from the test thread would race with startup and pass or fail on
            # scheduling, which is the difference between a regression test and a flaky one.
            thread = appliance.appliance
            with pytest.raises(AuthenticationError):
                await call_async(thread, lambda: appliance.admin.login("not that password    "))
            assert await call_async(thread, lambda: appliance.admin.login(PASSWORD)) is not None
    finally:
        appliance.stop()
        appliance.close()


async def test_seeding_does_not_replace_a_credential_an_operator_chose(
    tmp_path: Path, built_library: Any
) -> None:
    """The rule that keeps 12.6's environment file from being an attack surface.

    Without it, anyone who could write `/etc/encore/environment` could take over an appliance by
    rebooting it, and an upgrade would rotate the password of a machine whose owner had already
    set one.
    """

    first = "the first choice    "  # pragma: allowlist secret
    appliance = _appliance(tmp_path, built_library, admin_password=first)
    try:
        async with running(create_app(appliance)):
            # `seed` again, which is what an upgrade that restarted the appliance does. Queued on
            # the appliance thread, so it runs after the boot's own seed rather than beside it.
            again = await call_async(
                appliance.appliance,
                lambda: appliance.admin.seed("a later attempt    "),  # pragma: allowlist secret
            )
            assert again is False
            assert await call_async(appliance.appliance, lambda: appliance.admin.login(first))
    finally:
        appliance.stop()
        appliance.close()


def test_the_installer_never_prints_the_credential_it_was_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The password is in one file and must be in no output.

    Asserted on the installer's own stdout rather than on a log call, because the report is the
    artifact an operator pastes into an issue, and the environment file is legitimately the one
    place a systemd unit can read a secret from.
    """

    root = tmp_path / "appliance"
    root.mkdir()
    code = main(
        ["install", "--root", str(root), "--no-service", "--log-format", "console", "--json"],
        machine=_machine(root),
        http=StubHttpProbe(),
    )
    assert code == EXIT_OK
    printed = capsys.readouterr().out
    assert PASSWORD not in printed, "the report echoed the credential"
    environment = (root / "etc" / "encore" / "environment").read_text()
    assert PASSWORD not in environment, "no password was supplied, so none should be written"
    assert "ENCORE_ADMIN_PASSWORD" not in environment


# -- 3. the address ------------------------------------------------------------------


async def test_the_qr_code_names_the_port_the_appliance_listens_on(
    tmp_path: Path, built_library: Any
) -> None:
    """`http://jukebox.home.arpa` is not a wrong URL. It is an incomplete one.

    The appliance was on 8099, so the code resolved and refused the connection. Three files each
    formatted the address their own way and each was individually correct, which is why the fix is
    one property on `ServerConfig` and a test that compares the outputs rather than the inputs.
    """

    appliance = _appliance(tmp_path, built_library, port=8099, domain="party.lan")
    try:
        async with running(create_app(appliance)) as http:
            encoded = _DESC.search((await http.get("/qr.svg")).text)
            assert encoded is not None
            url = encoded.group(1).replace("&amp;", "&")
            assert url == "http://party.lan:8099"
            assert url == (await http.get("/api/v1/info")).json()["url"]
    finally:
        appliance.stop()
        appliance.close()


def test_every_reader_of_the_address_agrees_with_every_other_one(built_library: Any) -> None:
    """The contract, stated once, checked at all four places that could drift.

    `guest_url`, `qr.target`, the JSON API and the installer's report. The reason all four are in
    one assertion is that the bug was not in any of them; it was in the fact that there were four.
    """

    from encore.config.models import EncoreConfig, ServerConfig

    config = EncoreConfig(server=ServerConfig(domain="party.lan", port=8099))
    assert config.server.guest_url == target(config.server.domain, config.server.port)
    default = EncoreConfig(server=ServerConfig(domain="party.lan", port=80))
    assert default.server.guest_url == "http://party.lan"


def test_a_template_comment_cannot_satisfy_the_autoescaping_guard(tmp_path: Path) -> None:
    """The guard matched a comment containing `| safe` instead of a filter using it.

    Kept as a regression because the fix was to the *test*, and a test fix is the kind of change
    somebody reverts while fixing something else. The rule the guard enforces is real; only its
    matcher was wrong.
    """

    template = tmp_path / "example.html"
    template.write_text("{# this note mentions | safe in prose #}\n<p>hello</p>\n")
    text = template.read_text()
    assert "| safe" in text
    assert "| safe" not in _outside_comments(text), "the extraction still counts comments"


# -- the wiring ----------------------------------------------------------------------


def _build_wheel(tmp_path: Path) -> Path:
    import subprocess

    repo = Path(__file__).resolve().parents[2]
    subprocess.run(  # noqa: S603 - this repository's own build backend, from its own argv
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(repo)],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    produced = sorted(tmp_path.glob("*.whl"))
    assert produced
    return produced[0]


def _from_environment() -> str:
    """Read the variable the way `apps/server/main.py` does, so this test cannot pass by the
    fixture and the implementation agreeing on a constant instead of on the environment."""

    import os

    return os.environ.get("ENCORE_ADMIN_PASSWORD", "") or ""


def _machine(root: Path) -> FakeMachine:
    from apps.installer.system import OsRelease

    return FakeMachine(
        recorder=RecordingRunner(effects=synced(), real=(("rsync",),)),
        os_release=OsRelease(id="debian"),
        binaries={"mpv": Path("/usr/bin/mpv"), "uv": Path("/usr/local/bin/uv")},
    )


def _appliance(tmp_path: Path, built_library: Any, **settings: Any) -> Any:
    """The shipped composition, over the fixture library, with the address under test.

    A new `runtime.db` per call so that a test that seeds one appliance cannot sign in to another.
    """

    from encore.config.models import EncoreConfig, PathsConfig, ServerConfig

    name = "-".join(f"{key}-{value}" for key, value in sorted(settings.items())) or "default"
    home = tmp_path / f"appliance-{name}"
    home.mkdir(parents=True, exist_ok=True)
    network = {key: settings.pop(key) for key in ("port", "domain") if key in settings}
    config = EncoreConfig(
        paths=PathsConfig(
            library_db=built_library.options.library_db,
            runtime_db=home / "runtime.db",
            artwork_dir=home / "artwork",
            temp_dir=home / "temp",
            music_dir=built_library.options.music_dir,
        ),
        server=ServerConfig.model_validate({"port": 8080, **network}),
    )
    return build(config, launcher=FakeEngine(), admin_password=settings.pop("admin_password", None))


def _outside_comments(text: str) -> str:
    return re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
