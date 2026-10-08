# Task Template — Documentation

Docs are deliverables, not decoration (AEP 11, AIG 20).

```text
Task:
<One sentence: who can now do or understand what they could not before.>

Audience:
<guest | host/administrator | contributor | packager | integrator | AI session>

Documents touched:
- <paths, e.g. docs/User-Guide.md, docs/api/README.md, docs/adr/ADR-00X-….md>

Fact source of truth:
<SAPRS chapter/section, ADR, or code that was read — never memory.>

Out of scope:
<Code changes. If the code is wrong, that is a separate task.>

Acceptance criteria:
- A reader can complete <task> using only this document
- Every command shown exists, or is labelled as arriving in milestone N
- All relative links resolve (tools/check_links.py passes)
- The documentation map in docs/README.md reflects the change
```

## Rules

- Mark unimplemented commitments as **outline**; never write a doc that
  describes a feature as if it worked.
- One fact, one home: link instead of duplicating, and keep the precedence list
  in `docs/README.md` honest.
- New ADRs use the format in `docs/adr/README.md` (SAPRS 15.8) and are added to
  the index in the same PR.
- Vendored planning documents (`docs/SAPRS`, `docs/AIG`, `docs/AEP`,
  `docs/ProjectBootstrapKit_PBK.md`) are edited only by their authors, in a
  dedicated PR, never by a formatter.
- Run `uv run python tools/check_links.py` and `scripts/format.sh` before
  pushing; Markdown lint runs in the Documentation workflow.
