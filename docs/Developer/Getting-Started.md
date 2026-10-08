# Getting Started

Goal (PBK 15): clone the repository, install dependencies and pass the quality
gates in a few minutes.

## Prerequisites

- Git.
- [`uv`](https://docs.astral.sh/uv/) — the only Python tooling required.
  Install it once:

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

- Python 3.12+ is fetched by `uv` if your machine does not have it; the pin lives
  in `.python-version`. You never need to install Python by hand.
- Optional, for the markdown autofix in `scripts/format.sh`: Node 18+ (`npx`).
- Not needed for the bootstrap phase: mpv, audio hardware, a Raspberry Pi.

## First run

```bash
git clone https://github.com/clydepro/encore.git
cd encore
scripts/bootstrap.sh      # uv sync (editable install) + git hooks
scripts/check.sh          # everything CI will check
```

Expected output ends with `All local gates passed.`

## Daily loop

```bash
scripts/test.sh                    # unit + integration, fast
scripts/test.sh unit -k queue      # filter
scripts/format.sh                  # rewrite into canonical form
scripts/lint.sh                    # ruff, yamllint, mypy
scripts/check.sh --slow            # + performance and Party Simulation
uv run pre-commit run --all-files  # the hooks themselves
```

Hooks run on `git commit`. If a hook rewrites a file, review the diff and commit
again — that is the fast feedback PBK 5 asks for.

## Branch hygiene

```bash
git switch main && git pull --ff-only
git switch -c feat/123-queue-service
# ... commit with Conventional Commits (SAPRS 15.12) ...
git push -u origin feat/123-queue-service
gh pr create --fill
```

Commit prefixes: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `perf`,
with a scope matching an area label — `feat(queue):`, `fix(playback):`,
`docs(adr):`, `test(search):`.

## Layout

```text
.github/        CI, templates, labels, dependabot
apps/           builder + server entry points (empty until their milestones)
encore/         the Python package: api, config, controllers, domain, events,
                playback, repositories, search, services, templates, static,
                utilities
tests/          unit, integration, regression, performance, party_simulation
docs/           SAPRS, AIG, AEP, ADRs, Developer/, api/, images/
ai/             prompts, context, task templates, reviews, checklists
scripts/        setup, lint, format, test, check, run-server, run-builder
tools/          developer-only utilities, never shipped
assets/         logos and artwork source
examples/       example configuration
```

The layout is fixed by PBK Chapter 2; changing it requires an ADR.

## "It does not work on my machine"

1. `uv --version` — uv 0.5+ expected; `uv self update`.
2. `uv sync --frozen --all-groups` then `uv run python -c "import encore"`.
3. Delete `.venv/` and re-run `scripts/bootstrap.sh`.
4. `git status --ignored | head` — a stray `library.db`/`coverage.xml` is
   harmless; a stale `.mypy_cache` can be removed.
5. Open a [Question issue](https://github.com/clydepro/encore/issues/new/choose)
   with the exact command and output.

## Where to read next

- [Developer handbook](README.md)
- [Testing](Testing.md)
- [Continuous integration](Continuous-Integration.md)
- [ADRs](../adr/)
