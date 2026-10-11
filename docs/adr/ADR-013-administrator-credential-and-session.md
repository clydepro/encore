# ADR-013: The Administrator Credential Lives in `runtime.db`; The Session Lives in Memory

## Status

Accepted

## Date

2026-10-11

## Context

SAPRS 1.3 gives the administrator a list of capabilities and SAPRS 10.8 requires that
every one of them be behind authentication while guest endpoints stay open. The
credential itself was decided twice and left half-done:

- SAPRS 12.3 forbids configuration from becoming application-managed state, so a
  `password:` key in `config.yaml` is out — and `AdminConfig` is built to refuse such a
  key at startup, not merely to discourage it.
- SAPRS 5.7 puts "administrative state as required" in `runtime.db`, and ADR-009's
  second half gives that database a write path with migrations. Phase 2 wrote
  `AdminStateRepository` against that decision: one row, `CHECK (id = 1)`, a PBKDF2
  hash, and no credential anywhere in the configuration model.

What was never decided is the second half of a login: what a browser holds afterwards.
`ai/context/milestones.md` names it as an open question "to resolve before v1", and
`ai/HANDOFF.md` adds the constraint that matters — the administrative interface is the
first thing in Encore with state that is in neither database.

Four facts about the appliance decide it:

1. **There is one administrator** (SAPRS 12.3's "one administrative account", and
   `admin_state`'s table constraint enforcing it in DDL). Nothing here is a user system.
2. **There is one thread that owns `runtime.db`** (ADR-012). Anything a request needs to
   read on the way to deciding whether the request is allowed had better not be a
   database read, or every page view queues behind the party.
3. **The interface is HTTP on a LAN with no TLS.** SAPRS 13.1's supported platform is a
   Raspberry Pi serving phones over a local network; `docs/Administrator-Guide.md` says
   plainly that the box is not meant to face the internet. A cookie cannot be `Secure`
   on an appliance that has no certificate, so whatever the cookie carries must not be
   secret and must not be forgeable by someone who can read the LAN.
4. **Guests are anonymous by design** (ADR-007, and SAPRS 1.3's "guest anonymity must not
   be compromised by the administrative interface"). An administrative interface is the
   most likely place in the product to start recording who asked, because a dashboard
   wants to say "three people searched". That is the risk this decision has to close,
   not the password.

## Decision

**The credential is verified against `runtime.db` and the session is held in memory.**

- **Login** runs `AdminStateRepository.verify()` — a PBKDF2 hash comparison, and a read
  of the one-row `admin_state` — on the appliance thread, through the same `read()` every
  other service call crosses on (ADR-012). It is the only administrative action that
  touches a database, and it happens once per operator per day.
- **A session** is 32 bytes from `secrets.token_urlsafe`, mapped in an in-process
  dictionary to its expiry, guarded by a lock and pruned on every check. It is not a JWT,
  not signed, and not stored: the token *is* the capability, which is why the cookie is
  `HttpOnly`, `SameSite=Strict`, `Path=/`, and `Max-Age` equal to `admin.session_minutes`
  (SAPRS 12.2's one administrative setting).
- **Verification is memory-only and runs on the event loop**, not on the appliance
  thread. A dependency that read `runtime.db` would put a serialized database round trip
  in front of every admin page view for no additional safety, because the credential
  check has already happened.
- **Expiry is sliding**: an authenticated request refreshes the deadline, and 60 idle
  minutes end it. A restart ends every session, which is stated as a property rather
  than discovered as an outage: an appliance that rebooted at 3 a.m. should not leave a
  logged-in dashboard in somebody's browser until noon.
- **Logout revokes.** Because the mapping is server-side, forgetting one entry is the
  whole of logout; a bearer token that outlived the click that ended it would be a
  session that cannot be stopped.
- **CSRF is refused by cookie flags plus an origin check**, not by a token: a
  `SameSite=Strict` cookie is not sent on a cross-site request at all, and a state-
  changing request that carries an `Origin` from another host is answered `403`. There
  is no form on this appliance that a third-party page can submit with credentials
  attached, and a CSRF token in every admin form would be one more piece of state to
  get wrong. This is recorded here rather than in a code comment because it is the kind
  of decision that looks like an omission.
- **Failed attempts are throttled globally, not per address.** A sliding window of five
  failures per fifteen minutes locks the *appliance*, and the appliance holds no map of
  clients. Keying the window by IP would be a one-line change that ends ADR-007 — a
  table of who connected, kept by the interface that exists to not keep it — and the
  attacker this defends against is on the LAN and shares an address with the party
  anyway.
- **A machine client that is not a browser gets `401` and the error envelope**; a person
  who navigates to an admin page without a session gets `303` to `/admin/login`, exactly
  as `POST /queue` gives a browser a redirect and a tool JSON (SAPRS 10.7 names the 401,
  and it is still there for anyone who asks in a language that has status codes).
- **A first boot with no credential asks for one.** `/admin/login` renders a create-
  password form while `admin_state` is empty, the form is refused (`409`) the moment a
  row exists, and the event is logged at `WARNING`. Milestone 15's installer seeds the
  credential from `ENCORE_ADMIN_PASSWORD` before the service starts (SAPRS 12.6: an
  environment value, not a configuration file), so this path exists for a hand-built
  appliance rather than as the intended route. The trade — whoever speaks first to a
  fresh box owns it — is accepted deliberately: the alternative is an appliance whose
  administrative interface cannot be used at all until a later milestone lands.
- **Changing the password is an administrative operation** on an authenticated session,
  and it revokes every session including the one that made the request. A credential
  rotation that left live sessions valid would be a rotation with a delay nobody can see.
- **Nothing administrative is pushed to a guest.** The SSE fan-out stays exactly as
  ADR-012 describes it: `swap:player` and `swap:alerts`, both rendered from guest-visible
  state. The admin screens reuse those two regions and refresh their own, admin-only
  panels from a DOM event the live layer dispatches when a frame arrives. There is no
  `/admin/events`, no third region, and no session on the stream.

## Consequences

Positive:

- The guest half of the appliance is unchanged. No route, template, frame or table
  acquires an identity field, and `tests/unit/test_architecture_guardrails.py` keeps it
  that way by checking the services' dataclasses for the same markers it checks the
  domain's.
- A page view that is already authorized costs no database access beyond the reads the
  page would have done anyway. The budgets in SAPRS 1.8 stay the budgets.
- The credential survives a reinstall of the *code* and not of the *data*, which is the
  same lifetime `runtime.db` has (SAPRS 5.7's "may be rebuilt or migrated independently"),
  and a restored `runtime.db` restores the login it belongs to.
- Revocation is a dictionary entry, so logout, password change and restart are three ways
  of saying the same true thing rather than three mechanisms that can disagree.

Costs:

- **Sessions do not survive a restart.** An operator who restarts the appliance mid-party
  types the password again. Accepted: the alternative is persistence for a value whose
  only job is to avoid typing, and `runtime.db` is not a place to put things that want to
  look like cookies.
- **Sessions do not survive a second process.** `uvicorn --workers 2` would give each
  worker its own dictionary and half the logins would 401 at random. `apps/server/`
  already runs exactly one worker and says in `main.py` why (one mpv, one queue); this
  decision makes that constraint slightly stronger and slightly more visible, which is
  an improvement in a failure mode nobody wants.
- **In-memory state in a service is the sort of thing this project distrusts** — the
  AEP's rule is that mutable state belongs in `runtime.db` (AEP 18's spirit, SAPRS 12.4's
  list). A session is on that list's edge: it is runtime state, it is short-lived, and
  the specification's own examples of what belongs in the database are queue, history,
  statistics and "current operational state where persistence is required". Persistence
  is not required for a login. This ADR is the record that the sentence was read and the
  judgement made, and it is the sentence a future reviewer should disagree with if the
  judgement was wrong.
- **`SameSite=Strict` is a modern-browser defence and nothing more.** A client that
  ignores cookie attributes on a LAN where an attacker can already see the plaintext
  session token is outside this design's threat model, and the mitigation for that
  attacker is TLS, which SAPRS 13 defers. Stated rather than hidden: this interface is
  for a network you own.
- **A fresh appliance can be claimed by whoever reaches it first.** Logged at `WARNING`,
  refused once, and superseded by the installer's environment seeding.

## Alternatives considered

- **A password in `config.yaml`.** Rejected twice over: SAPRS 12.3 forbids
  configuration-managed state, `AdminConfig` has `extra="forbid"` precisely so the key
  cannot be added, and a file that a support zip collects is not a secret store.
- **bcrypt or argon2id for the hash.** Not chosen *by this ADR*. `AdminStateRepository`
  already hides the KDF behind a `PasswordHasher` protocol and carries the algorithm and
  its parameters in the stored string, so an old hash keeps verifying after the default
  changes. SAPRS 12.3 names both; neither is a dependency, PBKDF2-HMAC-SHA256 at 600,000
  rounds is in the standard library and is roughly OWASP's 2023 figure, and adding a
  compiled wheel to a Pi image to avoid a `hashlib` call is AEP 10's wrong trade. The
  installer may make argon2 the default; that is a one-class change and no schema change.
- **A signed, stateless cookie (an `itsdangerous` token).** Rejected: it needs a signing
  key with the same lifetime problem as the session it replaces, it cannot be revoked
  short of an expiry list — which is the rejected design with extra steps — and it adds a
  dependency. `secrets` and a dictionary are the smaller correct thing.
- **Sessions in `runtime.db`.** Rejected for ADR-012's reason: it puts a write on the
  request path of a single-writer appliance, for a value that must not survive the
  process. It also makes "the database is the state" true of something the specification
  does not list as state, and `runtime.db`'s migrations would then carry a table whose
  rows are meaningless after a reboot. The `admin_state` row is state; the session is not.
- **HTTP basic auth.** Rejected: the browser's prompt cannot be styled, cannot be
  logged out of, cannot expire, and a LAN with an unstyled password box on every page is
  what an appliance does to a guest who wandered onto the wrong URL.
- **Per-client rate limiting keyed on source address.** Rejected as an ADR-007 breach
  (see Decision).
- **A per-administrator CSRF token in every form.** Rejected: `SameSite=Strict` plus an
  origin check does the same job with no state, no token in a template, and no second
  path by which a legitimately-slow admin request can be told it is a forgery.
- **A second SSE stream for the admin screens.** Rejected: AIG 22 forbids polling where
  SSE is available, and the way to satisfy that without leaking admin markup to guests is
  to reuse the stream's *facts* rather than open a second one. Every extra region is
  another template render per fact for every screen in the room (ADR-012's fan-out
  arithmetic), and the administrator is one screen.

## Verification

- `tests/unit/test_admin_service.py` — login, expiry (sliding, and a restart), revocation
  on logout and password change, the first-run window, and the throttle window with an
  injected clock.
- `tests/unit/test_api_auth.py` — the cookie's attributes, the origin refusal, and the
  browser-redirect-versus-JSON-401 split.
- `tests/integration/test_admin_interface.py` — the composed appliance: a guest cannot
  reach any administrative route, an operator can drive every control the specification
  lists, and `admin_state` is the only table a login touches.
- `tests/e2e/test_admin_journey.py` — an operator logged in at one end of a room and a
  guest's phone at the other, watching the same two regions change.

## Related

- ADR-009 (split storage by mutability) — where the credential row lives.
- ADR-012 (one appliance thread) — why login crosses `read()` and session checks do not.
- ADR-007 (anonymous guests) — why the throttle has no client keys.
- ADR-006 (immutable library) — what `POST /admin/library/reload` may and may not do.
- SAPRS 1.3, 5.7, 8.8, 10.7, 10.8, 12.3, 12.6, 12.7. AIG 14.
