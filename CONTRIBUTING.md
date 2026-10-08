# Contributing to Encore

Welcome. Encore is a small appliance with unusually explicit documentation, and
the point of that documentation is that you should be able to contribute without
asking permission first.

- Reading order: [README](README.md) →
  [Getting started](docs/Developer/Getting-Started.md) →
  [SAPRS Chapter 1–2](docs/SAPRS/Encore-SAPRS.md) →
  [ADRs](docs/adr/README.md).
- Behaviour questions: [Documentation map](docs/README.md).
- Setup trouble: open a [Question issue](https://github.com/clydepro/encore/issues/new/choose);
  a confusing setup step is a bug in this file.

## 1. Ground rules

1. **Architecture first.** The guardrails in SAPRS 11.10 and AIG 4 are absolute
   and are enforced by tests. If a task seems to require violating one, the task
   is wrong or the guardrail needs a superseding ADR.
2. **Scope discipline.** Implement exactly the requested feature. No drive-by
   refactors, renames or formatting of code you did not have to touch (AEP 7).
3. **One change, one commit, one PR.** Each commit compiles and passes tests;
   incomplete work stays on a branch.
4. **Every feature ships with tests and documentation** (AEP 11, AEP 12).
   A bug fix ships with a regression test, always (SAPRS 14.14).
5. **Dependencies must be justified.** Standard library first; existing
   dependency second; new dependency with a written reason (AEP 10, SAPRS 15.10).
6. **Measure before optimizing** (AEP 14) against the SAPRS 1.8 targets.

## 2. Find something to work on

- [`good-first-issue`](https://github.com/clydepro/encore/labels/good-first-issue)
  — scoped and mentored.
- [`help-wanted`](https://github.com/clydepro/encore/labels/help-wanted).
- Milestone issues labelled by area (`area:playback`, `area:search`, …).
- If the feature is not in an issue, open one first. Design discussion happens
  in issues, not in PRs.

Comment to claim an issue; if it is `in-progress` someone else has it.

## 3. Set up

```bash
git clone https://github.com/clydepro/encore.git
cd encore
scripts/bootstrap.sh
scripts/check.sh
```

That installs dependencies from `uv.lock`, installs the git hooks, and runs the
full gate. If any of it fails on a fresh clone, that is a bug — please report it.

## 4. Make the change

```bash
git switch main && git pull --ff-only
git switch -c feat/123-queue-service     # or fix/, docs/, test/, refactor/, chore/
```

Workflow per AEP 5 — do not skip steps:

1. Read the task request.
2. Read the relevant SAPRS chapters.
3. Read the applicable ADRs.
4. Design: which service owns this? which modules, APIs, tests, docs change?
5. Implement.
6. Test.
7. `scripts/check.sh`.
8. Update documentation and `CHANGELOG.md`.
9. Commit, push, open the PR from the template.

### Commit messages

Conventional Commits with an area scope (SAPRS 15.12):

```text
feat(queue): add FIFO queue service
fix(playback): recover after mpv crash
docs(adr): document event bus architecture
test(search): add album lookup regression
```

The subject line says *what*; the body says *why* and links the issue.

### Python style

- Python 3.12+ syntax, type hints everywhere, `pathlib`, dataclasses/Pydantic,
  enums, context managers (SAPRS 15.5, AIG 17).
- Small functions, small classes, early returns, dependency injection,
  immutable data, composition, explicit names (AIG 9).
- No singletons, no magic values, no large utility modules, no circular imports.
- Public classes/functions/modules are documented; comments explain why
  (SAPRS 15.7).

Ruff formats and lints; do not fight it.

### Where code goes

| You are adding… | Put it in… |
| --------------- | ---------- |
| An entity or rule | `encore/domain/` |
| A capability with one owner | `encore/services/` |
| An event or the bus | `encore/events/` |
| SQL / ORM access | `encore/repositories/` |
| An HTTP or HTMX endpoint | `encore/controllers/` + `encore/api/` |
| mpv interaction | `encore/playback/` |
| A search query | `encore/search/` |
| A template | `encore/templates/` |
| A CSS/JS asset | `encore/static/` |
| A builder step | `apps/builder/` |
| A test double or helper | `tests/support/` |
| A maintainer script | `scripts/` or `tools/` |

## 5. Test

```bash
scripts/test.sh                    # unit + integration
scripts/test.sh unit -k search     # filtered
scripts/check.sh --slow            # + performance and Party Simulation
```

Minimum per change (AEP 12): unit tests always; integration tests when
collaboration is involved; end-to-end when runtime behaviour is visible; a
regression test whenever a defect is fixed.

Coverage moves toward 90% overall (SAPRS 14.16). Coverage is an indicator, not a
substitute for meaningful tests.

## 6. Documentation

Update whichever of these the change touches (AEP 11): user guide, administrator
guide, developer handbook, API reference, configuration examples, ADR, docstrings,
`CHANGELOG.md`. Code without documentation is incomplete — the PR will be sent
back, not finished for you.

New architectural decision → new ADR (format in
[docs/adr/README.md](docs/adr/README.md)); a change of an accepted decision means
superseding ADRs, never editing history.

## 7. Pull requests

Fill in the template: summary, motivation, testing performed, documentation
updated, SAPRS chapters affected, ADR required?, breaking change?. Keep it
focused; a PR that touches three areas is three PRs.

CI must be green before review. Reviews check the
[code review checklist](ai/checklists/code-review.md) and the guardrails.

What is actually enforced, for maintainers included (`enforce_admins`): the
fourteen required checks named in `.github/branch_protection.json`, linear
history, and `main` rejecting direct pushes — so work in a branch and open a PR.
Human review is a norm rather than a gate while the project has one maintainer,
because GitHub does not let an author approve their own pull request. Ask for
review explicitly on anything that changes behaviour, and run the AI review pass
below before a human looks. See
[repository administration](docs/Developer/Repository-Administration.md) for the
settings to raise once a second reviewer exists.

## 8. AI-assisted contributions

AI tools are first-class contributors here, and that is a responsibility, not an
exception (SAPRS 15.14):

- Read [`ai/current-phase.md`](ai/current-phase.md) and
  [`ai/HANDOFF.md`](ai/HANDOFF.md) first: what the last session did, decided and
  left open.
- Start the session from [`ai/prompts/standard-prompt-header.md`](ai/prompts/standard-prompt-header.md).
- Use a task template from [`ai/task_templates/`](ai/task_templates/).
- A human reviewed and understands every line, and says so in the PR.
- Run the AI review pass in [`ai/prompts/code-review.md`](ai/prompts/code-review.md)
  before requesting a human review.
- Respect what previous AI (or human) work already established (AEP 27): improve
  consistency, do not rewrite silently, document architectural concerns.
- Never let an AI invent architecture that conflicts with the SAPRS or an ADR.

## 9. Definition of done (AIG 20)

- [ ] Code implemented.
- [ ] Tests pass, and new behaviour is covered.
- [ ] Type checking and linting pass (`scripts/check.sh`).
- [ ] Documentation updated.
- [ ] No architectural guardrail violated.
- [ ] Acceptance criteria in the issue satisfied.
- [ ] `CHANGELOG.md` entry added.
- [ ] `ai/current-phase.md` and `ai/HANDOFF.md` rewritten for the next session,
      in the same commit — a handoff note that describes a phase already merged is
      worse than none.

## 10. Community

Be kind, be specific, assume good faith, and remember there is a party happening
on the other end of this software. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Security issues go through [SECURITY.md](SECURITY.md) instead of a public issue.

Thank you for making the music better.
