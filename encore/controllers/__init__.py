"""The human interface: pages, fragments, and the one thing a guest can do (SAPRS 9).

Three modules, split by what a route returns rather than by what it reads:

* `browse.py` — the shell, with a different middle: `/`, `/search`, `/artists`,
  `/artists/{id}`, `/albums/{id}`, and `/artwork/{kind}/{id}`.
* `panels.py` — the same regions again at `/fragments/...`, so a browser that has been
  asleep can ask for what it can see instead of guessing.
* `queue.py` — `POST /queue`, the appliance's only guest write.

A controller reads through the services and never past them: no SQL, no repository, no
mpv. `read()` in `encore/api/deps.py` is the doorway, and ADR-012 is why it exists — a
route that called `QueueService` directly would be issuing mpv commands from the event
loop, sharing one socket with the tick that is doing the same thing.

The admin interface (SAPRS 8.8, AIG step 14) will live beside these as `admin.py`, with
its own authentication dependency. Nothing in this package should be assumed public once
that exists; everything in it is public today because a guest's screen is the point.
"""

__all__: list[str] = []
