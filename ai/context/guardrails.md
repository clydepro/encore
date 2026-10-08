# Context Bundle — Guardrails and Prohibitions

For sessions that touch boundaries. Citations are the authority; this page is the
quick index.

## Mandatory guardrails (SAPRS 11.10, AIG 4)

Enforced in CI by `tests/unit/test_architecture_guardrails.py`:

1. Domain services do not import FastAPI.
2. Domain services do not depend on HTMX or Jinja.
3. Controllers do not access SQLite directly.
4. Repositories do not contain business rules.
5. Event handlers do not invoke other event handlers.
6. Playback does not know about HTTP.
7. The Library Builder does not import runtime playback.
8. The runtime never modifies `library.db`.
9. Templates contain presentation logic only.

## Never do these (AIG 22)

| Prohibition | Because |
| ----------- | ------- |
| No SPA framework, no React/Vue/Angular | ADR-002, SAPRS 3.2 |
| No runtime state inside `library.db` | ADR-006, SAPRS 5.2 |
| Do not expose SQLite to controllers | SAPRS 15.2 |
| Do not bypass the Event Bus | ADR-004, SAPRS 11.1 |
| No business logic in templates | SAPRS 11.10 |
| Do not couple Builder and Server | ADR-001 |
| No guest accounts or guest identity | ADR-007, AIG 22 |
| No polling where SSE is available | AIG 22 |
| No optimizing before measuring | AEP 14 |
| No unannounced architecture change | AEP 2, SAPRS 15.8 |

## Dependency policy (AEP 10, SAPRS 15.15)

Ask, in order: is the standard library sufficient → does an existing dependency
solve it → is the candidate actively maintained → does it simplify the project?
Licence must be permissive-compatible, and every addition is justified in the PR
body.

## Configuration and state (SAPRS 12)

- YAML at install time, validated at startup, never application-managed state.
- Never store in YAML: queue, playback state, statistics, user state, library
  state, arbitrary records.
- Never hard-code secrets; use environment or system mechanisms.
- Read-only configuration summary in the admin UI; no editing UI.

## Security invariants (AEP 17)

- Guests anonymous, administrators authenticated, least privilege, secure
  defaults.
- No logging of passwords, session cookies, personal data or configuration
  secrets.
- Guest anonymity must never be reconstructable from the admin surface.

## UI constraints (SAPRS 9, AEP 20)

Mobile-first, large touch targets, no horizontal scrolling, minimal typing, no
decorative animation, no clutter, accessible by default. HTMX attributes and
fragments over custom JavaScript.

## Performance budgets (SAPRS 1.8, AIG 19)

Search < 100 ms · queue op < 50 ms · navigation < 200 ms · playback start
~250 ms · SSE propagation < 1 s. Encoded in
`tests/performance/test_performance_targets.py`.

## If a task requires breaking one of these

Stop. State the conflict, cite both places, and propose the smallest
alternative — with an ADR if the decision really must change (AEP 4, AEP 28).
