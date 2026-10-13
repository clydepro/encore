# ADR-015: First-Boot Credential Seeding, and One Owner for the Guest Address

## Status

Accepted

## Date

2026-11-06

## Context

Milestone 15 has to make two promises that the code had already half-kept, and both broke in the
same way: a document said something happened, every test said it happened, and it did not.

**The credential.** ADR-013 decided that the operator's credential lives in `runtime.db` and that a
`password:` key in `config.yaml` is a startup error. It recorded a forward note — seed from the
environment "at install time, milestone 15". The Administrator Guide and SAPRS 12.6 both describe
`ENCORE_ADMIN_PASSWORD` as the way to configure an appliance before it boots. Nothing read the
variable. An operator who followed the documentation, set it, started the service, and typed the
password was told it was not right, on the machine they had just installed, in front of the guests
they had installed it for. Meanwhile `AdminService.setup()` was minting a random password and
printing it to the journal, so the appliance *was* secured and the guide was describing a feature
that existed only in prose.

**The address.** SAPRS 9.11 requires a QR code that takes a guest to the search page, and SAPRS
1.4 requires the appliance to be reachable at a name on the local network. The URL was built in
three places — `/api/v1/info`, the page footer, and the QR page — as three separate
`f"http://{domain}/"` expressions, all of which dropped the port. Encore's default port is 8080
(SAPRS 12.2), so the shipped configuration printed a code that resolved and refused the connection.
Each of the three files was correct about its own line; there was no place they could be wrong
together.

## Decision

### 1. The appliance seeds its own credential, on first boot, from the environment

`AdminService.seed(password)` exists, and `Appliance.become_ready()` calls it before anything else
in startup — on the appliance thread, because it is a write to `runtime.db` and ADR-012's rule is
what makes the rest of startup safe to reason about.

Three rules, and the third is the reason this is a method on the appliance rather than a line in
`build()`:

- **An existing credential always wins.** A row in `admin_state` defeats the environment file,
  silently, and the boot logs at DEBUG. Otherwise anyone who could write
  `/etc/encore/environment` could take over an appliance by rebooting it, and an upgrade would
  rotate the password of a machine whose owner had already changed it. `seed()`'s return value is
  the fact that this happened, so `encore-install verify` can name a stale line in a file instead
  of raising about it.
- **A value too weak to accept is reported and skipped, not fatal.** A jukebox that will not play
  because of a typo in an environment file is the worse failure: the first-run page is still the way
  in, and it warns as loudly as ADR-013 requires.
- **It mints no session.** Nobody is waiting for a login, and a token issued to a process with no
  client attached is a token in a log.

**The installer never writes `runtime.db` to seed.** It writes
`ENCORE_ADMIN_PASSWORD` into `/etc/encore/environment` at mode `0600` and that is the whole of its
part. The alternative — a root process deleting or inserting rows in a database owned by the service
account — is how an install ends with a `runtime.db` whose journal is owned by root, and the
appliance's next write to it is the failure. The one exception, in the other direction, is
`reset-credential`, which deletes the row after stopping the service; see ADR-014's debt list.

Rejected: putting the credential in `config.yaml` (SAPRS 12.3 and `AdminConfig`'s `extra="forbid"`
both refuse it); requiring a flag (`--admin-password` would put the appliance's only credential in
every user's `ps` output and in somebody's shell history); writing the hash at install time (above);
refusing to start when the variable is missing (an appliance with no administrator is a worse
outcome than an appliance with a first-run page).

### 2. One property owns the guest address, and `/qr` is server-side

`ServerConfig.guest_url` is the only expression of "where a guest points their browser". It omits
the port exactly when the port is 80, and `encore.utilities.qr.target()` uses the same constant for
the same rule so the two cannot drift. `/api/v1/info` reads the property, the footer reads the
property, `encore-install`'s report reads the property, and `tests/regression` asserts the four
agree.

The QR code itself is rendered server-side with `segno` (BSD, pure Python, no transitive
dependencies) as SVG, at `GET /qr` (a printable page) and `GET /qr.svg` (the image). It is **public
and cached** — `public, max-age=86400` — unlike every other dynamic route:

- Public, because the address of the jukebox is already on the footer of every guest page and in
  `/api/v1/info`; printing it discloses nothing. Behind the session cookie it would be a code only
  the operator can print, which is the wrong person on a chair with a printer.
- Cached, because SAPRS 10.10's two rules ("never cache mutable state", "cache artwork") both
  misfit it. A QR code is immutable until an operator edits `server.domain` and restarts, which is
  the lifetime of a static asset, and `no-store` would mean every phone in the room re-requesting an
  SVG while somebody is looking for a wall.
- **Parameterless.** There is no `?url=`. A QR endpoint that encoded a caller-supplied string is an
  appliance that will print a phishing code about itself, and the page is public.

Rejected: a hand-written encoder (a QR symbol is data encoding, Reed-Solomon over GF(256), mask
selection by penalty score, and a format string, in that order, and a mistake in any of the four
produces an image that looks correct and does not scan — a bug that surfaces at a party);
`qrcode`/Pillow raster output (a printed code is a vector problem; a resampled PNG is the classic
non-scanning poster); browser-side generation (SAPRS 12.3's offline rule plus a CDN, which is two
rules); and encoding the *bind* address (`server.host` defaults to `0.0.0.0`, and `0.0.0.0` in a QR
code is a code that scans to nowhere).

## Consequences

**Added.** `AdminService.seed()`, `ServerConfig.guest_url`, `encore/utilities/qr.py`,
`encore/controllers/qr.py`, `encore/templates/pages/qr.html`, and a `segno` runtime dependency.

**Gained.** An appliance can be configured for production without a browser, which is what
13.11's acceptance list implicitly assumes when it checks "an administrator credential exists".
A printed poster and a machine cannot disagree. Two findings that documentation claimed were
features stop being able to ship silently: both now have boundary tests, in files that would have
been green before the fix and are red after any regression.

**Accepted.** The runtime now has a path that writes `admin_state` without an operator pressing a
button, so "credential creation" needs its rules stated in one place rather than in a route; that
place is `seed()`, and `setup()` calls the same private helper. `segno` is one more dependency in a
product whose install must work offline; it is pure Python, it vendors nothing, and the alternative
was worse.

**Technical debt, recorded deliberately.**

- The credential's *strength* is a minimum byte length in the service. A future dictionary check
  belongs in `_acceptable()` and not in the installer, or the two will disagree about whether a
  given `ENCORE_ADMIN_PASSWORD` is seedable.
- `guest_url` assumes HTTP. A TLS-capable Encore would need a scheme decision in the same property,
  and the QR unit tests would then be the thing that caught a footer that still said `http://`.
- Caching the QR for a day means a `server.domain` edit is invisible to a browser that already
  fetched the poster, until the cache expires. That is the correct trade for a page fetched twice a
  year, and it is the sort of thing a person reprints rather than debugs.

## References

- SAPRS 12.6 (secrets), 12.2 (`server.port`, `server.domain`), 9.11 (the QR code), 1.4 (LAN
  addressing), 10.10 (cache policy), 13.7 (the one command), 13.11 (acceptance list), 8.8 (the
  administrative session).
- ADR-013 (the credential model this seeds into), ADR-012 (which thread may write), ADR-014 (which
  application may write at all), ADR-008 (offline as a requirement).
- Issue #32. `encore/services/admin_service.py`, `apps/server/appliance.py`,
  `encore/config/models.py`, `encore/utilities/qr.py`, `encore/controllers/qr.py`,
  `tests/regression/test_issue_32_installer.py`, `tests/integration/test_qr_routes.py`.
