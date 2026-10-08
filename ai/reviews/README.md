# AI Review Notes

This directory holds the output of AI review passes
([`../prompts/code-review.md`](../prompts/code-review.md)), one file per review:

```text
YYYY-MM-DD-<issue-or-pr>-<slug>.md
```

Reviews are recorded, not discarded, because "what did we know before merging?"
is worth answering later. Do not commit reviews that contain secrets, personal
data or library contents from a real installation.

- [`report-template.md`](report-template.md) — the shape every review follows.
- Files here are notes, not normative documents. ADRs and the SAPRS outrank them
  (AEP 2).
