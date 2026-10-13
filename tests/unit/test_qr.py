"""QR codes: what they say, how they render, and what may not vary between renderings.

SAPRS 9.11 asks for a printable code that opens the guest interface. The interesting part is not
the encoding — `segno` does that — but the two claims underneath it: that the address in the code
is the address the appliance actually answers on, and that the image is stable enough to cache for
a day on a wall.
"""

from __future__ import annotations

import io
import re
from typing import Final

import segno

from encore.config.models import ServerConfig
from encore.utilities.qr import QUIET_ZONE, SCALE, svg, target

URL: Final = "http://jukebox.home.arpa:8080"


# -- what the code says -----------------------------------------------------------


def test_a_non_default_port_is_part_of_the_address() -> None:
    """The bug this file exists to prevent: a code that scans to a refused connection.

    The appliance's default port is 8080, so a target built from a hostname alone would be wrong
    on every installation the specification describes — and correct in exactly the case nobody
    runs, which is the one a hand-written template would remember.
    """

    assert target("jukebox.home.arpa", 8080) == "http://jukebox.home.arpa:8080"


def test_the_default_http_port_is_not_repeated() -> None:
    assert target("jukebox.home.arpa", 80) == "http://jukebox.home.arpa"


def test_a_custom_port_survives_a_custom_domain() -> None:
    assert target("music.local", 9000) == "http://music.local:9000"


def test_the_target_agrees_with_the_configuration_models_own_url() -> None:
    """One rule for the address, in one place (issue #32's finding about `/api/v1/info`).

    `ServerConfig.guest_url` is what the footer, `/api/v1/info` and the QR page all read. If it
    ever disagrees with `target()`, three places in the product start telling guests three things.
    """

    for port in (80, 8080, 8099):
        config = ServerConfig(port=port, domain="jukebox.home.arpa")
        assert target(config.domain, config.port) == config.guest_url


def test_a_bind_address_never_becomes_a_host_name() -> None:
    """`0.0.0.0` in a QR code scans to nowhere, and `server.host` is where it would come from."""

    assert target(ServerConfig().domain, ServerConfig().port) == "http://jukebox.home.arpa:8080"
    assert "0.0.0.0" not in target(ServerConfig().domain, ServerConfig().port)  # noqa: S104


# -- what the code is -------------------------------------------------------------


def test_the_document_is_what_segno_writes_for_the_same_url() -> None:
    """The rendering is the library's, with the parameters this module names.

    A decoder would be the stronger claim, and `segno` 1.6 does not ship one without an optional
    image dependency this appliance has no reason to carry. What can be asserted is that `svg()`
    is not a second encoder: it hands the payload to `segno.make` and renders with the scale and
    border documented below, so a mistake here is a mistake about the parameters, not about
    Reed-Solomon arithmetic, which is the part worth refusing to write by hand.
    """

    for payload in (URL, "http://jukebox.home.arpa", "http://music.local:9000"):
        symbol = segno.make(payload, error="M", boost_error=False)
        expected = io.BytesIO()
        symbol.save(
            expected, kind="svg", scale=SCALE, border=QUIET_ZONE, title="Encore", desc=payload
        )
        assert svg(payload) == expected.getvalue().decode("utf-8")


def test_the_document_is_a_standalone_svg() -> None:
    text = svg(URL)
    assert text.startswith("<?xml")
    assert "<svg" in text
    assert text.rstrip().endswith("</svg>")


def test_the_quiet_zone_is_rendered() -> None:
    """Four modules of border is the difference between a label that scans and one that does not.

    Measured out of the document rather than read off the constant, because the constant is what
    the printer was told and the width is what the printer will see.
    """

    symbol = segno.make(URL, error="M")
    # 17 + 4·version is the standard's own formula for a symbol's side, and the version follows
    # from how much data went in — so this says the whole URL is in the picture. A truncated
    # payload would render a smaller symbol at the same scale, and every phone in the room would
    # shrug at it.
    version = symbol.version
    assert isinstance(version, int), f"segno reported the symbol version as {version!r}"
    edge = 17 + 4 * version + 2 * QUIET_ZONE
    match = re.search(r'width="(\d+)"', svg(URL))
    assert match is not None
    declared = int(match.group(1))
    assert declared == edge * SCALE


def test_the_same_payload_always_renders_the_same_bytes() -> None:
    """The cache header on `/qr.svg` promises a day; this is what makes the promise safe."""

    assert svg(URL, title="Encore") == svg(URL, title="Encore")


def test_a_different_payload_renders_a_different_document() -> None:
    assert svg(URL) != svg(URL.replace("8080", "8081"))


def test_the_image_carries_an_accessible_name() -> None:
    """A QR code is the least readable way to write an address; a screen reader needs the words."""

    text = svg(URL, title="Encore — scan to queue a song", description=URL)
    assert "<title>Encore — scan to queue a song</title>" in text
    assert f"<desc>{URL}</desc>" in text


def test_the_description_defaults_to_the_address() -> None:
    assert f"<desc>{URL}</desc>" in svg(URL)


def test_the_scale_is_the_print_size_and_not_the_display_size() -> None:
    """One scale, chosen so an `<img>` with no width fits a phone and still prints at A5.

    Asserted as a property of the document rather than of the constant: a call site that passed
    `scale=` would otherwise change what a wall poster looks like while the tests went on passing.
    """

    assert 'transform="scale(8)"' in svg(URL)
