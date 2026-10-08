# ADR-002: HTMX Instead of SPA

## Status

Accepted

## Date

2026-10-07

## Context

The guest experience is used on phones over a shared wireless network, from a
device that may be several rooms from the access point, and in a room where
forty people may load the page at once. Encore has exactly two screens' worth
of state to move around: browse, search, queue, now playing, up next.

SAPRS 3.2 forbids a SPA framework, client-side routing, React, Vue and Angular.
The remaining question is what to use instead, given that the interface still
has to feel like one continuous application (SAPRS 9.1) and update live.

## Decision

Server-rendered Jinja2 templates styled with Tailwind, enhanced with **HTMX**
for partial updates and **Server-Sent Events** for live state.

- The server owns the shell and the fragments; HTMX swaps fragments in place.
- One persistent `EventSource` stream drives Now Playing and Up Next (SAPRS 10).
- No client-side router, no client-side store, no build step for application
  JavaScript.
- JavaScript is added only where HTMX cannot do the job; "Do not add JavaScript
  when HTMX is sufficient" is a review question, not a suggestion.

## Consequences

Positive:

- The first paint is a template render; guests on weak signal get usable UI
  quickly, and a slow phone is not penalised with a large JS bundle.
- HTML stays the API: server-rendered fragments are trivially testable and the
  same endpoints compose with the versioned JSON API.
- Fewer moving parts on a 4 GB target device and in the contributor's head.

Costs:

- Interaction richness is bounded by what fragments can express.
- Server round-trips for each interaction; endpoint latency budgets
  (navigation < 200 ms) become hard requirements rather than guidelines.
- The team must resist the gravitational pull of "just add a little React".

## Alternatives considered

- **React/Vue/Svelte SPA with a JSON API.** Rejected: a bundle to build, a
  client-side cache to keep coherent with playback state, and memory cost on
  every guest device, in exchange for interactions HTMX already covers.
- **Alpine.js sprinkles over server templates.** Rejected as a default: it
  reintroduces a client state model. Allowed only for narrowly scoped input
  affordances.
- **Plain full-page form posts with polling.** Rejected: violates AIG 22
  ("no polling where SSE is available") and makes live queue updates sluggish.
- **WebSockets instead of SSE.** Rejected for v1: Encore's real-time need is
  one-way fan-out, where SSE gives reconnection semantics for free.
