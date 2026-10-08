# ADR-007: Anonymous Guest Model

## Status

Accepted

## Date

2026-10-07

## Context

Encore runs on a private LAN at a party, reached by scanning a QR code. Guests
are holding someone else's phone, sometimes with a screen lock they should not
have to defeat. Any identity requirement — account, name, passcode, cookie
profile — adds friction to the one action the product exists to make
effortless, and adds personal data the appliance has no reason to hold.

SAPRS 1.3 defines exactly two roles: anonymous Guest and authenticated
Administrator. AIG 22 forbids guest accounts outright.

## Decision

Guests are **anonymous by design**.

- No registration, no profile, no per-guest storage, no client fingerprint.
- Anyone who can reach the appliance on the LAN can browse, search and queue.
- The queue is strictly FIFO, visible to everyone, duplicates allowed, and it
  deliberately records *what* was queued, never *who* queued it (SAPRS 8).
- Administrative functions live behind a separate authenticated surface
  (SAPRS 1.3, Chapter 9); admin authentication never becomes a guest prompt.
- Anonymity is a promise, not an accident: the administrative interface must
  not reconstruct or display guest identity, and the UI may not use per-device
  identifiers for convenience features.

## Consequences

Positive:

- Onboarding is one scan and one tap, on any device, with no data retention.
- GDPR-adjacent comfort: the appliance stores musical facts, not people.
- The queue's fairness rules are auditable by anyone in the room, which is the
  social mechanism that replaces moderation tooling.

Costs:

- No per-guest favourites, history or "resume where you left off" in v1 —
  deferred to SAPRS 16.3 where they must be redesigned without breaking this
  promise.
- Abuse control is physical/network-level (LAN scope, queue depth caps, rate
  limiting in configuration), not identity-level.
- Guests cannot undo a queue entry they added, since nothing links an entry to
  a submitter.

## Alternatives considered

- **Optional guest names.** Rejected: turns attribution into a preference, and
  attribution is precisely what the party experience benefits from avoiding.
- **Device fingerprint or cookie per guest.** Rejected: reintroduces identity
  and retention through a side door.
- **Ephemeral sessions with a queue token.** Rejected for v1: solves "undo",
  costs a state model, and invites later "who was that?" features.
- **Room codes or shared passphrases for guests.** Rejected as a default: LAN
  scope already defines the trust boundary; extra steps hurt the primary flow.
- **OAuth login.** Rejected: a cloud dependency in an explicitly local
  appliance.
