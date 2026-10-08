"""Apply the GitHub label taxonomy from `.github/labels.yml` (PBK Chapter 10).

Labels are repository metadata rather than files, so the taxonomy lives in the
repository and this tool pushes it. Existing-but-undeclared labels are reported
and only removed with `--prune`, because closed issues keep their labels and a
typo is cheaper to fix than to explain.

Usage:

    uv run python tools/sync_labels.py [owner/repo] [--prune] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
TAXONOMY = ROOT / ".github" / "labels.yml"


def load_taxonomy() -> list[dict[str, str]]:
    """Flatten the grouped taxonomy file into GitHub label records."""

    with TAXONOMY.open(encoding="utf-8") as handle:
        document: dict[str, list[dict[str, str]]] = yaml.safe_load(handle)

    labels: list[dict[str, str]] = []
    for group in document.values():
        labels.extend(group)
    names = [label["name"] for label in labels]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SystemExit(f"duplicate label names in {TAXONOMY}: {duplicates}")
    return labels


def gh(*args: str, repo: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the GitHub CLI, tolerating 'already exists' style failures."""

    command = ["gh", *args]
    if repo:
        command += ["--repo", repo]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def repository_from_origin() -> str:
    """Infer `owner/repo` from the current git remote."""

    origin = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not origin:
        raise SystemExit("no git remote named origin; pass owner/repo explicitly")
    tail = origin.split(":", 1)[-1] if "@" in origin else origin.split("github.com", 1)[-1]
    parts = tail.strip("/").removesuffix(".git").split("/")
    if len(parts) < 2:
        raise SystemExit(f"cannot infer owner/repo from remote {origin!r}")
    return "/".join(parts[-2:])


def existing_labels(repo: str) -> dict[str, dict[str, Any]]:
    result = gh("api", "labels", "--paginate", "--jq", ".", repo=repo)
    if result.returncode != 0:
        raise SystemExit(f"could not read labels: {result.stderr.strip()}")
    listed: list[dict[str, Any]] = json.loads(result.stdout or "[]")
    return {str(item["name"]): item for item in listed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", default="", help="owner/repo (default: from origin)")
    parser.add_argument("--prune", action="store_true", help="delete undeclared labels")
    parser.add_argument("--dry-run", action="store_true", help="print actions only")
    args = parser.parse_args(argv)

    if shutil.which("gh") is None:
        raise SystemExit("gh (GitHub CLI) is required: https://cli.github.com")

    repo = args.repo or repository_from_origin()
    desired = load_taxonomy()
    present = existing_labels(repo)

    for label in desired:
        name, colour, description = (
            label["name"],
            label["color"],
            label["description"],
        )
        if name not in present:
            print(f"  create {name}")
            if not args.dry_run:
                result = gh(
                    "label",
                    "create",
                    "--force",
                    "--name",
                    name,
                    "--color",
                    colour,
                    "--description",
                    description,
                    repo=repo,
                )
                if result.returncode != 0:
                    print(f"  failed {name}: {result.stderr.strip()}", file=sys.stderr)
                    return 1
            continue
        if (
            present[name].get("description") != description
            or str(present[name].get("color", "")).lower() != colour.lower()
        ):
            print(f"  update {name}")
            if not args.dry_run:
                gh(
                    "label",
                    "edit",
                    "--name",
                    name,
                    "--color",
                    colour,
                    "--description",
                    description,
                    repo=repo,
                )

    managed = {label["name"] for label in desired}
    for name in sorted(set(present) - managed):
        if name.startswith(("dependencies", "documentation", "good first issue", "help wanted")):
            continue  # GitHub defaults; harmless to leave in place
        print(f"  undeclared: {name}" + (" (would delete)" if args.prune else ""))
        if args.prune and not args.dry_run:
            gh("label", "delete", name, repo=repo)

    print(f"Labels synchronised for {repo}. Verify: gh label list --repo {repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
