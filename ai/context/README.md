# Context Bundles

Short, citation-carrying summaries of the project for AI sessions. Use the
smallest bundle that covers the task; attach the SAPRS sections it names when the
task touches that area.

| Bundle | Use when |
| ------ | -------- |
| [architecture.md](architecture.md) | Any feature or service work. |
| [guardrails.md](guardrails.md) | Anything near a boundary, dependency choice, config, security or UI rule. |
| [tooling-and-repository.md](tooling-and-repository.md) | CI, scripts, packaging, templates, docs, test harness changes. |
| [milestones.md](milestones.md) | Every session — it says what already exists and what must not be built yet. |

Rules for these files:

- Summaries only; the SAPRS and the ADRs stay authoritative (AEP 2).
- Under ~120 lines each, so they cost little context.
- Update in the same PR that changes the thing described. A stale bundle is a
  defect: it will produce confident, wrong code.
