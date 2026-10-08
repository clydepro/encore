# ai/

Version-controlled working material for AI-assisted development of Encore.

The project expects AI contributions (SAPRS 15.14) and treats them like any
other contributor's: same standards, same guardrails, same tests, and a human who
understands every line. This directory is what makes that repeatable instead of
improvised.

```text
ai/
├── prompts/         session openers and review prompts
├── context/         compact, authoritative summaries of the architecture
├── task_templates/  the standard shape of a coding request
├── reviews/         review output and conventions
└── checklists/      gates an AI-assisted change must clear
```

## How to use it

1. Open a session with [`prompts/standard-prompt-header.md`](prompts/standard-prompt-header.md).
2. Attach the smallest [`context/`](context/) bundle that covers the area.
3. Write the task with [`task_templates/feature.md`](task_templates/feature.md)
   (or bug fix / refactor / documentation).
4. Implement, then run [`prompts/code-review.md`](prompts/code-review.md) as a
   *separate* pass before a human looks at it (AEP Appendix C).
5. Close with [`checklists/definition-of-done.md`](checklists/definition-of-done.md).

## Rules

- Nothing in `ai/` is authoritative. Precedence is: task request → SAPRS → AIG →
  ADRs → AEP (AEP 2). A context bundle that disagrees with the SAPRS is a bug in
  the bundle.
- Context bundles are summaries with links, never copies. Keep them under ~200
  lines and quote chapter/section identifiers rather than text.
- When a bundle goes stale, fix the bundle in the same PR that changed the
  architecture.
- Prompts here encode project values: say what is in scope, what is out of scope,
  and which documents apply.
