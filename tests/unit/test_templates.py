"""The templates as a contract, checked without a browser (SAPRS 13, ADR-002).

Two things are asserted here, and both are things that cannot be seen from a Python module:
that the htmx attributes in the markup are words htmx actually understands, and that nothing in
a template reaches for the internet.

Neither is exotic. The first exists because `hx-swap="find .row-status"` looks exactly like the
kind of thing htmx does — `find` is real, and it belongs in `hx-target` — and a swap style that
is not a swap style means the row's feedback silently stops appearing in a browser, in a test
suite with no browser. The second exists because an appliance in a basement with no DNS is the
deployment SAPRS 13 describes, and a single `<script src="https://…">` in one template would
work perfectly on every development machine and produce a blank page at the party.

These are parsed as text rather than rendered: the point is the attribute strings a reader
wrote, and rendering would only show the ones a code path happened to reach.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from encore.api.html import build_renderer

TEMPLATES: Final = Path(__file__).resolve().parents[2] / "encore" / "templates"
STATIC: Final = Path(__file__).resolve().parents[2] / "encore" / "static"

#: The swap styles htmx's own dispatcher knows (its `doSwap` switch). Anything else is a
#: registered strategy from an extension, and Encore vendors no extension.
SWAP_STYLES: Final = frozenset(
    {
        "innerHTML",
        "outerHTML",
        "beforebegin",
        "afterbegin",
        "beforeend",
        "afterend",
        "delete",
        "none",
    }
)

#: The leading token of each supported `hx-target` form. `find` and `closest` take a selector;
#: the rest are single words. An `#id` or a CSS selector is the other accepted shape.
TARGET_KEYWORDS: Final = frozenset(
    {"find", "closest", "next", "previous", "this", "global", "window"}
)

#: Attribute names Encore's templates are allowed to use. A new one is a decision about the
#: front end's surface area, and `docs/Developer/Frontend.md` is where it gets written down.
#:
#: `hx-confirm` joins the list with the administrative interface, for one button: "clear the
#: queue" is the only operator action no undo returns, and a native confirm dialog is the only
#: confirmation that cannot drift out of step with the page behind it — no component, no state,
#: nothing to re-render when the queue moves while the dialog is open.
ALLOWED_ATTRIBUTES: Final = frozenset(
    {
        "hx-get",
        "hx-post",
        "hx-target",
        "hx-swap",
        "hx-swap-oob",
        "hx-trigger",
        "hx-params",
        "hx-confirm",
    }
)

#: The regions `encore/static/js/encore-live.js` will try to swap, and therefore the ids that
#: must exist in the panel templates. A name in one list and not the other is a frame that goes
# nowhere and a panel nobody redraws.
REGIONS: Final = frozenset({"player", "alerts"})

mark = pytest.mark.unit

ATTRIBUTES = re.compile(r"""\b(hx-[a-z-]+)\s*=\s*"([^"]*)\"""")
JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
URLS = re.compile(r"(?:src|href)\s*=\s*[\"']https?://", re.IGNORECASE)


def templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def files() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for path in templates():
        text = path.read_text(encoding="utf-8")
        # A Jinja expression can contain a quoted `hx-…` inside a conditional attribute block;
        # stripping them keeps this about the literal markup a browser will parse.
        out.append((path, JINJA.sub(" ", text)))
    return out


def test_every_template_is_read() -> None:
    """A guardrail that finds no files is a guardrail that passes forever."""

    assert len(templates()) >= 12, "the template tree moved, and this check moved with it"


@pytest.mark.parametrize(("path", "text"), files(), ids=[path.name for path, _ in files()])
def test_htmx_attributes_say_words_htmx_knows(path: Path, text: str) -> None:
    for name, value in ATTRIBUTES.findall(text):
        assert name in ALLOWED_ATTRIBUTES, f"{path.name}: {name} is outside the vocabulary"
        if name == "hx-swap":
            style = value.split(":", 1)[0].strip()
            assert style in SWAP_STYLES, (
                f"{path.name}: hx-swap={value!r} is not a swap style. `find …` belongs in "
                "hx-target; a style htmx does not know means the swap never happens."
            )
        if name == "hx-target":
            tokens = [t for t in value.split() if t]
            assert tokens, f"{path.name}: empty hx-target"
            head = tokens[0]
            if head in TARGET_KEYWORDS:
                if head in {"find", "closest"}:
                    assert len(tokens) > 1, f"{path.name}: {head} needs a selector"
                continue
            assert head.startswith(("#", ".")) or value.strip() in {
                "this",
                "window",
            }, f"{path.name}: hx-target={value!r} is neither a selector nor a keyword"


def test_no_template_reaches_the_internet() -> None:
    """SAPRS 13's offline rule, asserted on the files rather than believed about them."""

    offenders = [path.name for path, text in files() if URLS.search(text)]
    assert not offenders, f"templates must load nothing but the appliance: {offenders}"


def test_our_javascript_talks_only_to_the_appliance() -> None:
    """The same rule, one layer down. Relative is the whole requirement.

    `/fragments/player` is this box. `//fonts.example.com` is not, and neither is
    `https://anything`, and the difference is one character — which is why this checks the
    scheme rather than banning `fetch`, which the reconnect path legitimately uses.
    """

    absolute = re.compile(r"[\"'`](?:https?:|//)[^\s\"'`]+")
    for path in sorted(STATIC.rglob("*.js")):
        if "vendor" in path.parts:
            continue
        hits = absolute.findall(path.read_text(encoding="utf-8"))
        assert not hits, f"{path.name} reaches somewhere that is not this appliance: {hits}"


def test_the_regions_the_script_names_are_the_regions_the_templates_define() -> None:
    """The two lists are one fact, held in two files that can disagree."""

    script = (STATIC / "js" / "encore-live.js").read_text(encoding="utf-8")
    declared = re.search(r"const REGIONS = \[([^\]]*)\]", script)
    assert declared is not None, "encore-live.js stopped declaring its regions"
    named = frozenset(value.strip().strip("\"'") for value in declared.group(1).split(","))
    assert named == REGIONS, f"the script and this file disagree: {sorted(named)}"

    body = "\n".join(text for _, text in files())
    for region in REGIONS:
        assert f'id="{region}"' in body, f"no template defines the region {region!r}"


def test_no_template_turns_autoescaping_off() -> None:
    """Every value in every template is escaped, which is the claim CodeQL asks about.

    A scanner that sees `Response(content=template.render(...))` and a request parameter
    somewhere upstream is right to be suspicious: it is a *sink* with a source flowing to it,
    and the only thing between them is a configuration flag on a Jinja environment two files
    away. `api/html.py` sets that flag with `select_autoescape`, which decides by filename —
    so the guarantee holds exactly as long as no template opts out locally, and an opt-out is
    one `| safe` away from a working feature.

    This is the assertion that keeps the difference visible. It is not a substitute for the
    escaping (that is the environment's job) and it is not a suppression of the alert either:
    `html.py` carries the reason in its own comment, and this is the reason's evidence.
    """

    opted_out = [
        f"{path.relative_to(TEMPLATES)}:{number}"
        for path, text in files()
        for number, line in enumerate(text.splitlines(), start=1)
        if re.search(r"\|\s*safe\b|{%\s*autoescape\s+false", line)
    ]
    assert not opted_out, f"templates that escape themselves: {opted_out}"

    renderer = build_renderer()
    escaped = renderer.render(
        "partials/error.html",
        {
            "failure": _Failure("<script>alert(1)</script>"),
            "details": {
                "fields": [{"location": ["body", "song_id"], "message": '<img src=x onerror="1">'}]
            },
        },
    )
    assert "<script>" not in escaped, "a value reached a page without escaping"
    assert "&lt;script&gt;" in escaped
    # The value survives as text in an attribute context rather than becoming a tag: `onerror`
    # is still readable in the page source, and `<img` is not there to execute.
    assert "onerror" in escaped
    assert "<img" not in escaped


class _Failure:
    """The shape `errors.respond()` passes to the error templates: a `code` and a `message`.

    Constructed here rather than imported from `encore.api.errors` because this file is about
    what a template does with a string, and the string is the interesting part.
    """

    def __init__(self, message: str, code: str = "invalid_request") -> None:
        self.message = message
        self.code = code
