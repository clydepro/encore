"""A QR code, as the appliance renders it (SAPRS 9.11, 1.3).

`segno` does the encoding; this module does the two things a template cannot be trusted with —
deciding what the code says, and rendering it as something a browser and a laser printer both
understand.

**Why a dependency and not a function.** A QR symbol is a data block, Reed-Solomon error
correction over GF(256), a mask chosen from eight candidates by a penalty score, and a format
string, in that order — and a mistake in any one of the four produces an image that looks
exactly like a QR code and decodes to nothing. That is the class of defect that costs a party an
hour, because the only test for it is a phone camera pointed at paper. AEP 10 asks for a reason
before a dependency, and the reason is that the alternative is hand-rolling error-correction
arithmetic to encode one URL. `segno` is pure Python, has no dependencies of its own, and is
BSD-licensed.

**Why SVG.** The code is both printed and displayed, and a vector is the only format that is
sharp at 300 dpi on paper and on a phone held at arm's length. It is also text, so the
determinism claimed below is checkable in a test rather than argued.

**Why the quiet zone stays.** Four modules of white border is the standard's minimum and the
difference between a code printed to the edge of a label and one that scans. It is left at the
default for exactly that reason, and a test asserts it rather than trusting the default to
never change.
"""

from __future__ import annotations

import io
from typing import Final

import segno

__all__ = ["MEDIA_TYPE", "SCALE", "svg", "target"]

#: What the code is asked to survive. `M` handles a scuff and a printer that is out of
#: calibration; `H` would survive a coffee ring and need a bigger symbol for a URL this short,
#: which is the wrong trade on an A5 sheet taped to a wall.
ERROR_CORRECTION: Final = "M"

#: Modules per side at render time. A print size, not a display size — a browser scales an SVG
#: to whatever box it is put in, and 8 keeps the whole symbol under 300 units across so an
#: `<img>` with no width set does not overflow a phone.
SCALE: Final = 8

#: The white border, in modules. Four is the standard's minimum.
QUIET_ZONE: Final = 4

#: What `GET /qr.svg` answers with.
MEDIA_TYPE: Final = "image/svg+xml"

#: Nothing listens on 443 — a LAN appliance speaks HTTP (SAPRS 1.4) — and a code that says
#: `https://` scans to a connection error on a network with no certificate authority.
SCHEME: Final = "http"

#: The port a URL does not print. Public because `ServerConfig.guest_url` uses it, and the whole
#: argument for one place is that "is this port implicit?" is one rule that four readers ask about.
HTTP_DEFAULT_PORT: Final = 80


def target(host: str, port: int) -> str:
    """The URL the code should encode, from what the appliance is configured with.

    The port is in the answer whenever it is not 80. That is the rule a browser applies to a
    typed address, and the mistake an appliance makes when it prints a code built from a
    template rather than from its own configuration: `http://jukebox.home.arpa` on a box
    listening on 8080 is a refused connection and a guest who assumes the jukebox is broken.

    Args:
        host: `server.domain`, the name guests resolve. Not `server.host`, which is a bind
            address, and `0.0.0.0` in a QR code is a code that scans to nowhere.
        port: `server.port`.
    """

    return f"{SCHEME}://{host}" if port == HTTP_DEFAULT_PORT else f"{SCHEME}://{host}:{port}"


def svg(payload: str, *, title: str = "Encore", description: str = "") -> str:
    """One QR code, as a standalone SVG document.

    Deterministic by construction: the same payload in, the same bytes out, which is what makes
    the long cache lifetime in `encore/controllers/qr.py` a fact rather than a hope. An operator
    who changes `server.domain` gets a different document, not a different rendering of the same
    one, and `tests/unit/test_qr.py` asserts both halves of that sentence.

    Args:
        payload: Usually `target(...)`. Anything else is a bug above this line.
        title: The accessible name of the image, for a reader that announces it.
        description: The URL in words, for the same reader: a QR code is the least readable
            possible way to write an address, and the text beside it on the printed sheet is
            what the `<desc>` says.
    """

    symbol = segno.make(payload, error=ERROR_CORRECTION, boost_error=False)
    buffer = io.BytesIO()
    symbol.save(
        buffer,
        kind="svg",
        scale=SCALE,
        border=QUIET_ZONE,
        title=title,
        desc=description or payload,
    )
    return buffer.getvalue().decode("utf-8")
