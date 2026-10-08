# Review Report Template

```markdown
# Review: <PR # or commit> — <one line>

- Date: YYYY-MM-DD
- Reviewer: <model + harness, and the human who requested it>
- Inputs: diff, issue <#>, SAPRS chapters <…>, ADRs <…>
- Verdict: approve | approve with nits | request changes

## Findings

| # | Severity | Location | Finding | Rule cited |
| - | -------- | -------- | ------- | ---------- |
| 1 | blocking | path:line | what is wrong | SAPRS 11.10 / ADR-004 / AEP 12 |

Severity: `blocking` (guardrail, correctness, security, missing regression
test) · `should` (test or documentation gap, unclear naming) · `nit`
(clarity, judgement call).

## Architectural check

- [ ] No coupling introduced around the Event Bus
- [ ] No business logic in controllers, repositories or templates
- [ ] `library.db` untouched at runtime; mutable state in `runtime.db`
- [ ] Builder and Server still independent
- [ ] Playback unaware of HTTP; presentation absent from the domain
- [ ] Guest anonymity preserved; no secrets in code, config or logs

## Testing gaps

<Specific tests that should exist, named as files under tests/.>

## Documentation gaps

<Which guide, API page or ADR must change, and why.>

## Performance and reliability

<Latency budget impact, blocking calls, N+1 queries, unbounded queues, SSE
fan-out, mpv restart behaviour.>

## Recommended order of changes

1. <first, because it unblocks or de-risks the rest>
2. …

## Explicitly not recommended

<Refactors out of scope, dependencies rejected, cleverness declined — with the
rule that declined it.>
```

## Notes for the reviewer model

- Cite or label "judgement"; an uncited style opinion is not a finding.
- Do not rewrite code in the report. Describe the change and let the author do
  it (AEP 27).
- If you cannot tell whether a decision conflicts with an ADR, say so and ask a
  maintainer rather than guessing (AEP 28).
