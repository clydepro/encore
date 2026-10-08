# Standard Prompt Header

Use this at the start of every AI coding session on Encore
(AEP Appendix A). It is short on purpose: the documents it points at carry the
detail.

```text
You are contributing to the Encore project.

Follow the SAPRS, AI Implementation Guide, ADRs and AI Engineering Playbook.
Respect all architectural guardrails.
Implement only the requested task.
Write production-quality Python 3.12 code.
Provide comprehensive tests.
Update documentation as required.
Do not violate Clean Architecture.
Do not bypass the Event Bus.
Explain any ambiguity before coding.
```

## Document paths to reference

| Document | Path |
| -------- | ---- |
| SAPRS | `docs/SAPRS/Encore-SAPRS.md` |
| AI Implementation Guide | `docs/AIG/AIImplementationGuide_AIG.md` |
| AI Engineering Playbook | `docs/AEP/AIEngineeringPlaybook_AEP.md` |
| Bootstrap kit (repo/tooling rules) | `docs/ProjectBootstrapKit_PBK.md` |
| ADR index | `docs/adr/README.md` |
| AGENTS.md (harness entry point) | `AGENTS.md` |

## Session preamble that works in practice

Add to the header above, per task:

```text
Repository facts you can rely on:
- Layout is fixed by PBK Chapter 2; do not create new top-level directories.
- Tooling: uv + pyproject.toml, Ruff (lint+format), strict MyPy, pytest with
  category markers, pre-commit, five GitHub Actions workflows.
- Tests live in tests/{unit,integration,regression,performance,party_simulation}
  and doubles in tests/support/. Markers are applied by directory.
- Run scripts/check.sh before declaring completion.
```

## Guardrails to repeat verbatim in every session (SAPRS 11.10)

- Domain services never import FastAPI or HTMX-specific code.
- Controllers never access SQLite directly.
- Repositories never contain business rules.
- Event handlers never invoke other handlers directly.
- Playback never knows about HTTP.
- The Builder never imports runtime playback components.
- The runtime never modifies `library.db`.
- Templates contain presentation only.

## Never-do list (AIG 22)

No SPA framework. No runtime writes into `library.db`. No SQLite exposed to
controllers. No bypassing the Event Bus. No business logic in templates. No
coupling Builder to Server. No guest accounts. No polling where SSE is
available. No premature optimization. No unannounced architecture changes.

## If the model is unsure

Say so, name the conflict, cite both places, propose the smallest reversible
option and wait (AEP 28). Incorrect certainty is worse than acknowledged
uncertainty.
