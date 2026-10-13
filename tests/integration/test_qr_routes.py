"""`GET /qr` on a running appliance (SAPRS 9.11, 1.4, issue #32).

The handler is three lines, so nothing here is about rendering. The question the file exists for
is whether the address a guest is *pointed at* is the address the appliance *answers on* — which
is a question about four modules agreeing (`config/models.py`'s `guest_url`, `utilities/qr.py`,
`api/v1.py`, `templates/shell.html`), and is exactly the kind a unit test answers badly, because
each of those four is correct on its own.

So the code is read back out of the SVG and compared against the URL the JSON API reports and the
URL the poster tells a human to type. Every disagreement is a machine whose guests scan a code and
get a connection error, in a room, in front of other people.

The appliance under test is the shipped composition (`apps/server/appliance.build`) behind the
routers `create_app` installs, for the reason `test_server_app.py` gives: an endpoint registered by
hand in a test would keep passing when the real application forgot the router, and forgetting a
router is precisely the bug this milestone can have.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from apps.server.appliance import Appliance, build
from encore.api.app import create_app
from encore.config.models import EncoreConfig, PathsConfig, QueueConfig, ServerConfig
from encore.utilities.qr import target
from tests.support.http import running
from tests.support.mpv import FakeEngine

pytestmark = pytest.mark.integration

_DESC = re.compile(r"<desc>(.*?)</desc>", re.DOTALL)

#: The operator's password, and the name keeps `detect-secrets` off the line.
PASSWORD = "an appliance password"  # pragma: allowlist secret


def _encoded(svg: str) -> str:
    """The URL the code encodes, recovered from the SVG rather than trusted from the handler.

    `segno` writes a `<desc>` containing the payload, so the string is recoverable without a QR
    decoder — and recovering it rather than recomputing it is the point. An assertion against
    `target(config.server.domain, config.server.port)` here would pass even if the route encoded
    something else, because test and implementation would be calling the same function.
    """

    found = _DESC.search(svg)
    assert found is not None, "the SVG says nothing about what it encodes"
    return found.group(1).replace("&amp;", "&")


def _appliance(encore_home: Path, built_library: Any, name: str, **server: Any) -> Appliance:
    """The shipped composition, over the fixture library.

    `name` picks a different `runtime.db` per address, because this file needs several appliances
    and a shared runtime database would make an admin session in one test a session in another.
    The library is shared: it is read-only by contract, and one Builder run costs the suite less
    than any of these tests would.
    """

    home = encore_home / "qr" / name
    home.mkdir(parents=True, exist_ok=True)
    return build(
        EncoreConfig(
            paths=PathsConfig(
                library_db=built_library.options.library_db,
                runtime_db=home / "runtime.db",
                artwork_dir=home / "artwork",
                temp_dir=home / "temp",
                music_dir=built_library.options.music_dir,
            ),
            queue=QueueConfig(max_items=8),
            # 8080 rather than an ephemeral port: 8080 *is* the shipped default (SAPRS 12.2), and
            # the assertion below is about a URL a guest would type into a browser.
            server=ServerConfig.model_validate({"port": 8080, **server}),
        ),
        launcher=FakeEngine(),
    )


@pytest.fixture
async def appliance(encore_home: Path, built_library: Any) -> AsyncIterator[Appliance]:
    made = _appliance(encore_home, built_library, "default")
    try:
        yield made
    finally:
        made.stop()
        made.close()


@pytest.fixture
async def client(appliance: Appliance) -> AsyncIterator[Any]:
    async with running(create_app(appliance)) as http:
        yield http


async def _one(encore_home: Path, built_library: Any, name: str, **server: Any) -> str:
    """The address a freshly composed appliance with this configuration puts in its code."""

    made = _appliance(encore_home, built_library, name, **server)
    try:
        async with running(create_app(made)) as http:
            return _encoded((await http.get("/qr.svg")).text)
    finally:
        made.stop()
        made.close()


class TestTheRoutes:
    async def test_the_page_is_public(self, client: Any) -> None:
        """No session, deliberately.

        The address is on the footer of every guest page, so printing it discloses nothing a guest
        does not already have; putting it behind the operator's cookie would make the only person
        able to reach it the person standing on a chair with a printer.
        """

        assert (await client.get("/qr")).status_code == 200

    async def test_the_image_is_svg(self, client: Any) -> None:
        response = await client.get("/qr.svg")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")

    async def test_both_are_cached_where_everything_else_is_not(self, client: Any) -> None:
        """SAPRS 10.10's two rules, neither of which fits a QR code on its own.

        Mutable state is `no-store`; artwork is cacheable. A code is immutable until a restart, so
        it belongs with the assets — and a poster that re-fetches on every phone in the room is a
        poster that fails at the moment it is most useful.
        """

        for path in ("/qr", "/qr.svg"):
            assert "max-age" in (await client.get(path)).headers["cache-control"], path
        assert "no-store" in (await client.get("/")).headers["cache-control"]

    async def test_the_page_references_the_image_rather_than_duplicating_it(
        self, client: Any
    ) -> None:
        """One encoder per response.

        A page that inlined the SVG and an endpoint that rendered it are two places that compute
        the same code, and the interesting property of `/qr` is that there is only one.
        """

        body = (await client.get("/qr")).text
        assert body.lstrip().startswith("<!doctype html")
        assert 'src="/qr.svg"' in body
        assert "<desc>" not in body

    async def test_the_image_is_self_contained(self, client: Any) -> None:
        """It has to be openable as a file, because somebody will save it rather than print it."""

        svg = (await client.get("/qr.svg")).text
        assert svg.lstrip().startswith("<?xml") or "<svg" in svg
        assert "xmlns=" in svg


class TestTheAddress:
    async def test_the_code_encodes_the_configured_address(self, client: Any) -> None:
        assert _encoded((await client.get("/qr.svg")).text) == "http://jukebox.home.arpa:8080"

    async def test_the_code_and_the_json_api_agree(self, client: Any) -> None:
        """The two machine-readable statements of where this box is.

        An integrator reading `/api/v1/info` and a wall printed from `/qr` disagreeing is the bug
        this file was written for.
        """

        info = (await client.get("/api/v1/info")).json()
        assert _encoded((await client.get("/qr.svg")).text) == info["url"]

    async def test_the_code_and_the_poster_agree(self, client: Any) -> None:
        """The text under a code is the fallback for the rain, and for a broken camera.

        9.11's requirement is that a guest can queue a song from a phone; the printed URL is the
        route that still works when the poster is unreadable.
        """

        assert _encoded((await client.get("/qr.svg")).text) in (await client.get("/qr")).text

    async def test_a_non_default_port_survives_the_whole_path(
        self, encore_home: Path, built_library: Any
    ) -> None:
        """The failure an appliance actually has.

        A `guest_url` that dropped a non-80 port would print a code for port 80, resolve, and
        refuse the connection — and pass on every developer's machine, which is a bug that ships.
        """

        assert await _one(encore_home, built_library, "8080", port=8080) == (
            "http://jukebox.home.arpa:8080"
        )

    async def test_the_configured_domain_reaches_the_code(
        self, encore_home: Path, built_library: Any
    ) -> None:
        assert (
            await _one(encore_home, built_library, "party", domain="party.lan", port=9001)
            == "http://party.lan:9001"
        )

    async def test_port_80_is_not_printed(self) -> None:
        assert target("jukebox.home.arpa", 80) == "http://jukebox.home.arpa"
        assert target("jukebox.home.arpa", 443) == "http://jukebox.home.arpa:443"

    async def test_no_parameter_can_make_it_encode_something_else(self, client: Any) -> None:
        """`?url=` is absent by design.

        A QR endpoint that encoded a caller's string is an appliance that will print a phishing
        code about itself, and this page is public.
        """

        for path in ("/qr.svg?url=http://example.invalid/", "/qr?domain=example.invalid"):
            assert "example.invalid" not in (await client.get(path)).text, path

    async def test_the_bind_address_is_never_the_encoded_one(self, client: Any) -> None:
        """`server.host` defaults to 0.0.0.0 (SAPRS 1.4), which is not a name anyone can open.

        The distinction lives in one attribute and one line of the controller; this is the test
        that keeps someone from "simplifying" it to `config.server.host`.
        """

        assert "0.0.0.0" not in (await client.get("/qr.svg")).text  # noqa: S104


class TestTheOperatorSeesIt:
    async def test_the_admin_navigation_links_to_it(
        self, encore_home: Path, built_library: Any
    ) -> None:
        """An undocumented feature is a feature nobody uses.

        The page is public, so the link can be a plain one; what matters is that the operator
        looking for "how do I put this on the wall" finds it from the dashboard rather than from
        the specification.
        """

        appliance = _appliance(encore_home, built_library, "admin-link")
        try:
            async with running(create_app(appliance)) as http:
                assert "no administrator yet" in (await http.get("/admin/login")).text
                created = await http.post(
                    "/admin/login",
                    data={"password": PASSWORD},  # pragma: allowlist secret
                    follow_redirects=False,
                )
                assert created.status_code == 303, created.text
                dashboard = await http.get("/admin/dashboard")
                assert 'href="/qr"' in dashboard.text
        finally:
            appliance.stop()
            appliance.close()
