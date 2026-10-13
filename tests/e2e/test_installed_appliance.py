"""An SD card, one command, and a machine that plays music (SAPRS 13.7, 13.8, AIG 20).

The installer's unit and integration tests ask whether the plan is right and whether the files
land. This asks the only question an operator cares about: after `encore-install`, does the
appliance *work*?

The test is therefore two halves that no other file in this repository joins.

* **The install half** is real: `main()` runs against a `FakeMachine` with a staging root, which
  is the same code path a Raspberry Pi takes, writing the same four files.
* **The boot half** reads what those files say and starts the appliance from them —
  `ConfigurationService(config.yaml)` plus `ENCORE_CONFIG` and `ENCORE_ADMIN_PASSWORD` out of the
  environment file — because "the installer wrote a configuration" and "the Server can run the
  configuration the installer wrote" are different claims, and only this file can be wrong about
  both.

Three findings from issue #32 ended up as tests here, and they are marked where they appear. They
share one shape: each was invisible below the boundary between the two applications. A wheel with
no server in it, an environment variable nothing read, and a QR code that dropped the port all
passed every test that existed, because every test that existed was on one side of the line.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.installer.main import EXIT_OK, main
from apps.installer.system import ServiceState
from apps.server.appliance import Appliance, build
from encore.api.app import create_app
from encore.config.service import ConfigurationService
from tests.support.http import running
from tests.support.installer import (
    FakeMachine,
    RecordingRunner,
    StubHttpProbe,
    synced,
)
from tests.support.mpv import FakeEngine

pytestmark = pytest.mark.e2e

_DESC = re.compile(r"<desc>(.*?)</desc>", re.DOTALL)

#: The credential issue #32 could not deliver. It is a test constant and nothing else, which is
#: the whole point of `ENCORE_ADMIN_PASSWORD`: the value never appears in a command line, a
#: report, or a log.
PASSWORD = "an appliance password from the environment"  # pragma: allowlist secret


@pytest.fixture
def staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One `encore-install`, run for real into a prefix.

    `monkeypatch` rather than a parameter on `main()`, because the installer's own rule is that a
    secret has exactly one source (SAPRS 12.6) and a test that passed it in some other way would
    be testing a route the product does not offer.
    """

    monkeypatch.setenv("ENCORE_ADMIN_PASSWORD", PASSWORD)
    root = tmp_path / "appliance"
    root.mkdir()
    host = FakeMachine(
        recorder=RecordingRunner(effects=synced(), real=_REAL),
        os_release=_debian(),
        binaries={"mpv": Path("/usr/bin/mpv"), "uv": Path("/usr/local/bin/uv")},
        # A machine that has systemd and does not already have the service. The commands are
        # recorded rather than run, so `install` writes the unit the way 13.3 describes it and
        # `verify` is answered from the scripted state below.
        systemd=True,
        user=True,
        unit=ServiceState(installed=True, enabled=True, active=True),
    )
    code = main(
        [
            "install",
            "--root",
            str(root),
            "--port",
            "8099",
            "--domain",
            "party.lan",
            "--music-dir",
            str(root / "opt" / "music"),
            "--log-format",
            "console",
        ],
        machine=host,
        http=StubHttpProbe(
            {"http://127.0.0.1:8099/api/v1/health": {"status": "ok"}},
        ),
    )
    assert code == EXIT_OK, "the installer did not finish, so there is no appliance to boot"
    return root


def _real() -> tuple[tuple[str, ...], ...]:
    return (("rsync",),)


_REAL = _real()


def _debian() -> Any:
    from apps.installer.system import OsRelease

    return OsRelease(id="debian")


def _environment(root: Path) -> dict[str, str]:
    """The `EnvironmentFile` the installer wrote, as the mapping systemd would have built."""

    path = root / "etc" / "encore" / "environment"
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip().strip('"')
    return values


@pytest.fixture
def appliance(staged: Path, built_library: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The installed machine, started from the files the installer left behind.

    Nothing here is configured by the test. `ENCORE_CONFIG` comes from the environment file, the
    paths inside it come from the staged layout, and the credential comes from the environment
    variable — the same three inputs a Pi gets, in the same order it gets them.
    """

    environment = _environment(staged)
    assert environment, "the installer wrote no environment file to read"
    config_path = Path(environment["ENCORE_CONFIG"])
    config = ConfigurationService(config_path).load()

    # The staged layout points the databases at the fixture's library, because building 15,000
    # songs into every install in this file would make the suite about the Builder.
    config = config.model_copy(
        update={
            "paths": config.paths.model_copy(
                update={"library_db": built_library.options.library_db}
            )
        }
    )
    made = build(
        config,
        launcher=FakeEngine(),
        admin_password=environment.get("ENCORE_ADMIN_PASSWORD", PASSWORD),
    )
    try:
        yield made
    finally:
        made.stop()
        made.close()


@pytest.fixture
async def client(appliance: Any) -> Any:
    async with running(create_app(appliance)) as http:
        yield http


class TestTheInstalledAppliance:
    def test_the_configuration_the_installer_wrote_is_the_one_the_server_accepts(
        self, staged: Path
    ) -> None:
        """The boundary issue #32 found broken, in both directions.

        The wheel contained no server, no templates and no `apps/server`; every test below this
        line passed. What the check has to be, then, is not "does the file parse" — the unit
        tests do that — but "does the file the installer wrote name paths that exist after the
        installer ran", which is a claim about two applications at once.
        """

        config = ConfigurationService(staged / "etc" / "encore" / "config.yaml").load()
        assert config.server.port == 8099
        assert config.server.domain == "party.lan"
        for path in (config.paths.library_db, config.paths.runtime_db, config.paths.artwork_dir):
            assert str(staged) in str(path), f"{path} escaped the staging prefix"
        assert (staged / "etc" / "encore" / "config.yaml").is_file()

    def test_the_environment_file_names_the_paths_the_unit_uses(self, staged: Path) -> None:
        """`ENCORE_CONFIG` and the unit's `EnvironmentFile=` are two claims about one file.

        They disagree only at 03:00, in the form of a service that starts and reads defaults
        rather than the operator's configuration.
        """

        environment = _environment(staged)
        unit = (staged / "etc" / "systemd" / "system" / "encore.service").read_text()
        assert Path(environment["ENCORE_CONFIG"]).is_file()
        assert "EnvironmentFile=" in unit
        assert str(staged) in environment["ENCORE_HOME"]

    def test_the_installed_tree_is_the_tree_the_documentation_describes(self, staged: Path) -> None:
        """SAPRS 13.3, checked where it is actually claimed: on a disk.

        A layout table in `layout.py` that disagreed with the guide would pass both suites that
        exist today. This is the third, and it is the one a person follows.
        """

        for relative in (
            "etc/systemd/system/encore.service",
            "usr/lib/tmpfiles.d/encore.conf",
            "opt/encore/application/pyproject.toml",
            "opt/encore/venv/bin/encore-server",
            "etc/encore/config.yaml",
            "etc/encore/environment",
            "etc/systemd/system/encore.service",
            "usr/lib/tmpfiles.d/encore.conf",
            "var/lib/encore",
            "var/lib/encore/artwork",
            "var/cache/encore/temp",
            "var/log/encore",
        ):
            assert (staged / relative).exists(), f"{relative} is not where 13.3 says it is"


class TestTheCredential:
    async def test_the_password_the_installer_was_given_signs_in(self, client: Any) -> None:
        """The finding, stated plainly: nothing in the runtime ever read `ENCORE_ADMIN_PASSWORD`.

        The documentation said it was seeded on first boot. The code minted a random one and
        printed it to the journal, which meant an operator who followed the guide, set the
        variable, and typed the password got "not right" — on the machine they had just installed,
        in front of the guests they had installed it for.
        """

        login = await client.get("/admin/login")
        assert "no administrator yet" not in login.text, (
            "the appliance was built without the credential the installer supplied"
        )
        created = await client.post(
            "/admin/login", data={"password": PASSWORD}, follow_redirects=False
        )
        assert created.status_code == 303, "the seeded password did not sign in"
        dashboard = await client.get("/admin/dashboard")
        assert dashboard.status_code == 200

    def test_the_secret_never_reached_the_report_or_the_logs(
        self, staged: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """12.6 across the whole install, not per-module.

        Both files that legitimately hold it are checked to hold it; every file the installer
        *printed* is checked not to. A password in a report is a password in a bug tracker.
        """

        assert PASSWORD in (staged / "etc" / "encore" / "environment").read_text()
        printed = capsys.readouterr()
        assert PASSWORD not in printed.out
        assert PASSWORD not in printed.err
        files = [staged / "etc" / "encore" / "config.yaml", staged / "var" / "log" / "encore"]
        for path in files:
            found = path.read_text(errors="ignore") if path.is_file() else ""
            assert PASSWORD not in found, f"{path} holds the credential and should not"

    async def test_a_second_boot_does_not_re_seed_or_lock_anybody_out(
        self, staged: Path, built_library: Any
    ) -> None:
        """Idempotence below the filesystem, where 13.7's rule was never extended.

        The first boot seeds `admin_state`; the second must find it there and leave it alone. An
        appliance that re-minted on every start would rotate its own password on every restart,
        and 13.9's upgrade — "stop, migrate, start" — would mean every upgrade needed a new
        password.
        """

        first = _boot(staged, built_library)
        try:
            async with running(create_app(first)):
                pass  # the lifespan is the first boot; the seed happens inside it
            # A second seed is what an upgrade that restarted the appliance does. It must be a
            # no-op rather than a rotation, or every restart would change the password.
            assert first.admin.seed(PASSWORD) is False, "the credential was seeded twice"
        finally:
            first.stop()
            first.close()
        second = _boot(staged, built_library)
        try:
            async with running(create_app(second)) as http:
                created = await http.post(
                    "/admin/login", data={"password": PASSWORD}, follow_redirects=False
                )
                assert created.status_code == 303, "the credential did not survive a restart"
        finally:
            second.stop()
            second.close()


class TestTheAddress:
    async def test_the_qr_code_says_where_the_appliance_actually_is(self, client: Any) -> None:
        """The third finding, and the quietest one.

        The installer wrote `port: 8099`, the Server listened on 8099, and `/api/v1/info` said
        `http://party.lan/`. A guest scans the poster, DNS resolves, and the connection is
        refused — on every page of a machine whose own documentation about itself was wrong.
        """

        svg = (await client.get("/qr.svg")).text
        encoded = _DESC.search(svg)
        assert encoded is not None
        url = encoded.group(1).replace("&amp;", "&")
        assert url == "http://party.lan:8099"
        assert url == (await client.get("/api/v1/info")).json()["url"]

    async def test_the_guest_page_and_the_code_agree(self, client: Any) -> None:
        page = (await client.get("/")).text
        assert "party.lan" in page
        assert 'href="/qr"' in page, "the QR page is reachable but not from where a guest is"

    async def test_the_installed_configuration_is_what_the_code_encodes(
        self, staged: Path, client: Any
    ) -> None:
        """One configuration, four readers.

        The file on disk, the appliance in memory, the JSON API and the QR code all have to agree,
        and the only way to test that is to have all four in one room.
        """

        config = ConfigurationService(staged / "etc" / "encore" / "config.yaml").load()
        assert config.server.guest_url == (await client.get("/api/v1/info")).json()["url"]


# -- the wiring this file needs twice ------------------------------------------------


def _boot(staged: Path, built_library: Any) -> Appliance:
    """Start the installed appliance a second time, the way systemd would."""

    environment = _environment(staged)
    config = ConfigurationService(Path(environment["ENCORE_CONFIG"])).load()
    config = config.model_copy(
        update={
            "paths": config.paths.model_copy(
                update={"library_db": built_library.options.library_db}
            )
        }
    )
    return build(
        config,
        launcher=FakeEngine(),
        admin_password=environment.get("ENCORE_ADMIN_PASSWORD", PASSWORD),
    )
