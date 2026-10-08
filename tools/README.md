# tools/

Small, single-purpose developer tools. Everything here is stdlib-plus-dev-deps,
typed, and safe to run from a fresh clone after `scripts/bootstrap.sh`.

| Tool             | Purpose                                    | Used by                  |
| ---------------- | ------------------------------------------ | ------------------------ |
| `check_links.py` | Verify internal Markdown links and anchors | `docs.yml` workflow      |
| `sync_labels.py` | Push `.github/labels.yml` to GitHub        | `scripts/sync-labels.sh` |

Rules for this directory:

- A tool is not application code and never ships to the appliance.
- One file, one job, `--help` describing usage, exit code 0 on success.
- If a tool grows options worth testing, it belongs in `encore/` instead.
