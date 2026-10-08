"""Check internal Markdown links and anchors (PBK 6 "Documentation").

Only *internal* links are verified: relative paths and same-file anchors. External
URLs are intentionally skipped here — CI should not fail because a third-party
site moved, and link-rot checks belong in a scheduled job if we ever want one.
Generated documentation is excluded by the same rule the workflow states.

Usage:

    uv run python tools/check_links.py [--verbose] [path ...]

Exit codes: 0 = all internal references resolve, 1 = at least one is broken.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]

SKIP_DIRECTORIES: Final[set[str]] = {".venv", "build", "dist", "site", "node_modules", ".git"}

#: No static site generator exists yet; the OpenAPI reference is generated in
#: milestone 11 and lands in a directory that this checker must not police.
GENERATED_DIRECTORIES: Final[set[str]] = {"generated"}

LINK_PATTERN: Final = re.compile(r"(!?)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
INLINE_LINK_PATTERN: Final = re.compile(r"<((?:https?|mailto):[^>]+)>")
FENCE_PATTERN: Final = re.compile(r"^\s*(```|~~~)")
ANTICODE_PATTERN: Final = re.compile(r"`[^`]*`")


def slugify(heading: str) -> str:
    """Approximate GitHub's anchor slug for a heading."""

    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text).strip("-")


def headings_in(path: Path) -> set[str]:
    """Collect anchor slugs defined by headings and explicit `<a name=...>`."""

    anchors: set[str] = set()
    inside_fence = False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return anchors
    for line in lines:
        if FENCE_PATTERN.match(line):
            inside_fence = not inside_fence
            continue
        if inside_fence:
            continue
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if match:
            anchors.add(slugify(match.group(1)))
        for explicit in re.findall(r"<a(?:nchor| name)=\"([^\"]+)\"", line):
            anchors.add(explicit)
    return anchors


def link_targets(markdown: str) -> list[str]:
    """Every link destination in a Markdown file, ignoring code."""

    stripped = INLINE_LINK_PATTERN.sub("", markdown)
    stripped = ANTICODE_PATTERN.sub("", stripped)
    cleaned_lines: list[str] = []
    inside_fence = False
    for line in stripped.splitlines():
        if FENCE_PATTERN.match(line):
            inside_fence = not inside_fence
            continue
        if not inside_fence:
            cleaned_lines.append(line)
    return [
        target
        for _image, target in LINK_PATTERN.findall("\n".join(cleaned_lines))
        if not target.startswith("#/")
    ]


def resolve(source: Path, target: str) -> tuple[Path | None, str]:
    """Split a link target into (file, anchor)."""

    raw_path, _, anchor = target.partition("#")
    if not raw_path:
        return None, anchor
    candidate = (source.parent / raw_path).resolve()
    if candidate.name in {"README", "index"} or candidate.suffix == "":
        for suggestion in (candidate / "README.md", candidate.with_suffix(".md"), candidate):
            if suggestion.is_file():
                return suggestion, anchor
        return candidate, anchor
    return candidate, anchor


def check_file(path: Path) -> list[str]:
    """Return human-readable problems found in one Markdown file."""

    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    for target in link_targets(text):
        if target.startswith(("http://", "https://", "mailto:", "tel:", "data:")):
            continue
        if re.match(r"^[a-zA-Z+.-]+:", target):
            continue
        file_target, anchor = resolve(path, target)
        if file_target is not None:
            if not file_target.exists():
                problems.append(f"{path.relative_to(ROOT)}: broken link -> {target}")
                continue
            if anchor and path.suffix == ".md" and anchor not in headings_in(file_target):
                problems.append(f"{path.relative_to(ROOT)}: missing anchor #{anchor} in {target}")
        elif anchor and anchor not in headings_in(path):
            problems.append(f"{path.relative_to(ROOT)}: missing same-file anchor #{anchor}")
    return problems


def discover(arguments: list[str]) -> list[Path]:
    if arguments:
        files: list[Path] = []
        for argument in arguments:
            path = (ROOT / argument).resolve()
            files.extend(sorted(path.rglob("*.md")) if path.is_dir() else [path])
        return files
    collected: list[Path] = []
    for path in sorted(ROOT.rglob("*.md")):
        parts = set(path.relative_to(ROOT).parts)
        if parts & (SKIP_DIRECTORIES | GENERATED_DIRECTORIES):
            continue
        collected.append(path)
    return collected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="files or directories to check")
    parser.add_argument("-v", "--verbose", action="store_true", help="list checked files")
    args = parser.parse_args(argv)

    failures: list[str] = []
    files = discover(args.paths)
    for path in files:
        if args.verbose:
            print(f"checking {path.relative_to(ROOT)}")
        failures.extend(check_file(path))

    print(f"checked {len(files)} Markdown file(s)")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"{len(failures)} broken internal reference(s)", file=sys.stderr)
        return 1
    print("all internal links and anchors resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
