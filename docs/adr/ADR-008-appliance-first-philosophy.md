# ADR-008: Appliance-First Philosophy

## Status

Accepted

## Date

2026-10-07

## Context

Most self-hosted media software is an application you operate: dashboards,
settings pages, dependency upgrades, log spelunking. Encore's stated purpose is
the opposite — "the music, not the software, should be the center of attention"
(SAPRS 1.1) — and its target hardware is a small ARM board that is expected to
be started once and then forgotten in a cupboard.

Every later decision in this project is downstream of how much operational
complexity we are willing to ask of a person who just wants music at a party.

## Decision

Encore is designed as an **appliance**.

- It starts automatically (`systemd`), needs no operator action to reach a
  working state, and stops only when told.
- Configuration is installation-time YAML, "set it and forget it" (SAPRS 12.2).
  The administrative interface may show a read-only configuration summary but
  must never edit or persist that file (SAPRS 12.3, 12.7).
- Expected failures are absorbed: mpv dies and is restarted, a browser
  disconnects, an artwork file is missing, a subscriber throws. A subsystem
  failure must not take down the appliance (SAPRS 11.9).
- Health is stated explicitly rather than inferred from logs, and diagnostics
  are readable by a non-developer holding a phone.
- Resource use is predictable and bounded: one process per role, local-only
  network exposure, no background cloud chatter, no indexes rebuilt during play.
- Installation and upgrade are one command, with manual steps documented as the
  authoritative path (SAPRS 13.2).
- The UI stays thin and quiet (ADR-002); the administrative surface exists to
  observe and intervene, not to be used.

## Consequences

Positive:

- Feature requests must survive the question "does this make the box more or
  less forgettable?", which keeps scope honest.
- Fewer knobs mean fewer untested combinations on odd hardware.
- Reliability work (supervision, recovery, health) is prioritised over surface
  area, matching SAPRS 16.2: stability over feature count.

Costs:

- Power users will ask for runtime-editable settings; the answer is a new file
  plus a restart, and that must be said plainly rather than worked around.
- Appliance behaviour needs packaging and systemd work that a "just run uvicorn"
  project skips.
- Debugging is remote and file-based, so structured logging and health output
  carry the load that an interactive admin console would otherwise share.

## Alternatives considered

- **Application-first: rich settings UI, plugin marketplace, runtime
  reconfiguration.** Rejected: turns the party box into something that requires
  tending, and multiplies untested state combinations.
- **Container-only deployment.** Rejected for v1: audio device ownership, ALSA
  or PipeWire routing and low-overhead service management are simpler on a
  documented systemd host; containers may supplement, never replace.
- **Cloud sync/remote access built in.** Rejected: adds an availability and
  privacy dependency the local-network model exists to avoid.
- **Config through environment variables only.** Rejected: no structure for
  audio output and playback options, and easy to half-set silently; YAML is
  validated at startup with a clear diagnostic instead (SAPRS 12.5).
