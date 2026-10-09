# Feature Implementation Checklist

Used before opening a PR for any new capability. Each line is a question with a
document behind it; a blank answer is an unfinished task.

## Before writing code

- [ ] Issue exists and its acceptance criteria are testable.
- [ ] SAPRS chapters read and cited (AEP 5).
- [ ] Applicable ADRs read; conflict identified or absence confirmed.
- [ ] Owning service identified — one responsibility (SAPRS 11.6, AIG 7).
- [ ] Modules, endpoints, events, tables and templates that will change, listed.
- [ ] What could regress, listed; which suite covers it, named.
- [ ] Out-of-scope items written down (AEP 7).

## Design

- [ ] Dependencies point inward: presentation → application → domain →
      repositories → infrastructure (SAPRS 15.2).
- [ ] Cross-service notification goes through the Event Bus, with an immutable,
      typed, timestamped event (AIG 8, ADR-004).
- [ ] New configuration is installation-time YAML, never application-managed
      state (SAPRS 12, ADR-008).
- [ ] Mutable state goes to `runtime.db` (SQLAlchemy transactions); `library.db`
      stays read-only and is reached through raw `sqlite3` (ADR-006, ADR-009).
- [ ] A store is verified by shape, not by handle: opening a missing SQLite file
      succeeds and yields an empty library, so existence, expected tables and a
      logged row count are required (ADR-009).
- [ ] No ORM object, `Session` or raw row crosses a repository boundary; repos
      return `encore.domain` types (ADR-009).
- [ ] No new dependency, or AEP 10's questions answered in the PR.
- [ ] Failure mode considered: what does the appliance do when this fails
      (SAPRS 11.9)?

## Implementation

- [ ] Type hints complete; Python 3.12 idioms, `pathlib`, dataclasses/Pydantic,
      enums (SAPRS 15.5, AIG 17).
- [ ] Constructor injection; no module-level state or singletons.
- [ ] Small functions, early returns, no hidden side effects (AIG 9).
- [ ] Errors recoverable where possible, actionable, and free of internals
      (AEP 15).
- [ ] Structured logging with no secrets or personal data (AEP 16).
- [ ] Performance target relevant to the feature respected and measured, not
      assumed (SAPRS 1.8, AEP 14).

## Tests

- [ ] Unit tests for the rule itself.
- [ ] Integration test for the collaboration (SAPRS 14.5).
- [ ] End-to-end/API test where behaviour is observable (SAPRS 14.6, 14.7).
- [ ] Regression test if a defect was fixed (SAPRS 14.14).
- [ ] Party Simulation impact considered for anything on the request path.
- [ ] No test requires mpv, audio hardware, network or a real library
      (SAPRS 14.4).

## Documentation

- [ ] User/administrator guide, API reference, developer docs as applicable
      (AEP 11).
- [ ] ADR added or superseded if the decision is architectural (SAPRS 15.8).
- [ ] `CHANGELOG.md` entry under Unreleased.
- [ ] Docstrings on new public objects.

## Gates

- [ ] `scripts/check.sh` green (pre-commit, Ruff, yamllint, MyPy, pytest,
      links).
- [ ] `scripts/check.sh --slow` green if runtime behaviour changed.
- [ ] CI green on the PR.
- [ ] AI review pass run when AI assistance was used
      (`ai/prompts/code-review.md`), result recorded in `ai/reviews/`.
- [ ] AIG 20 definition of done satisfied and stated in the PR.
