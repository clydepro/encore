# images/

Screenshots and diagrams referenced from the guides.

Planned contents (PBK 14; the README "Screenshots" section stays a placeholder
until these exist):

- `guest-now-playing.png` — mobile Now Playing.
- `guest-search.png` — search results, artist/album/song.
- `guest-up-next.png` — the queue.
- `admin-dashboard.png` — health, queue and statistics.
- `architecture.svg` — the SAPRS 2.2 diagram, redrawn.
- `qr-poster.svg` — onboarding poster.

Rules:

- PNG for UI captures, SVG for diagrams; keep each file under the pre-commit
  size limit and compress before committing.
- Name files `<subject>-<variant>.<ext>` and reference them with a relative path
  so `tools/check_links.py` can verify them.
- Never commit images containing real library data, artwork or hostnames.
