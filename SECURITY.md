# Security Policy

Encore is a local-network appliance: one process per role, no cloud dependency,
anonymous guests, authenticated administrators. Security work here means keeping
those properties true (AEP 17) — not adding accounts to a jukebox.

## Supported versions

| Version | Supported | Notes |
| ------- | --------- | ----- |
| 1.x | ✅ | Current stable appliance. |
| 0.x | ⚠️ | Development pre-releases; fixes land in `main` only. |
| < 0.x | ❌ | Bootstrap-era tags; not supported. |

Security fixes are backported to the newest supported minor release and, if the
issue is `High` or worse, to the previous minor as a patch release.

## Reporting a vulnerability

**Please do not use public issue templates for exploitable problems.**

1. Report privately via
   [GitHub Security Advisories](https://github.com/clydepro/encore/security/advisories/new)
   ("Report a vulnerability"). Private reporting keeps the repository history
   clean and gives maintainers time to patch before the party is interrupted.
2. If private reporting is unavailable to you, email the maintainers listed in
   [`.github/CODEOWNERS`](.github/CODEOWNERS) with the subject `Encore security`.
3. Include: version, platform, configuration deltas, and a reproduction. A
   failing test is ideal; a script is fine.

### What happens next

| Step | Target |
| ---- | ------ |
| Acknowledgement | within 5 business days |
| Triage and severity (CVSS v3.1) | within 14 days |
| Fix released and advisory published | within 60 days, sooner if feasible |
| Credit reporter (with consent) | in the advisory and `CHANGELOG.md` |

We ask for coordinated disclosure: no public detail before the fix ships, or
after 60 days from the report, whichever comes first.

## Scope: what counts as a vulnerability

- Any way for an unauthenticated LAN guest to gain administrative access.
- Anything that lets a guest de-anonymise another guest (ADR-007).
- Path traversal, SQL injection or command injection reaching `mpv`, the
  filesystem or either database.
- Writing to `library.db` from the runtime (ADR-006).
- Disclosure of the admin session secret, session cookies or log contents that
  should never contain them (SAPRS 12.6, AEP 16).
- SSRF or arbitrary outbound network from the Server; only the Builder may talk
  to MusicBrainz, and only when told to (ADR-001).
- Denial of service from ordinary guest actions such as duplicate queueing,
  which is a supported behaviour (SAPRS 8).
- A maliciously crafted audio file or metadata blob that escapes the playback
  sandbox.

## Not considered vulnerabilities

- A guest queuing songs the host dislikes; that is the product.
- Attacks requiring physical access to the device, or access to the LAN router
  or the host filesystem.
- Listening to unencrypted HTTP/SSE traffic on the local network. Encore's trust
  boundary *is* the LAN (ADR-007, ADR-008); TLS termination is a deployment
  choice and supported only as documented, optional hardening.
- Queue flooding that hits the configured `queue.max_items` limit.
- Vulnerabilities in your distribution's mpv, ALSA/PipeWire, ffmpeg or SQLite —
  report those upstream; we will patch our dependency ranges.
- Findings in the vendored planning documents under `docs/`.

## Hardening expectations for operators

- Keep the appliance on a trusted LAN segment; do not port-forward it.
- Set a strong administrator credential from a secret store, never in
  `/etc/encore/config.yaml` (SAPRS 12.6).
- Filesystem permissions for `/var/lib/encore` restricted to the `encore` user.
- Keep `mpv` and the OS patched; the release checklist verifies supported
  versions.
- Logs are structured and scrubbed; do not enable debug logging in shared
  environments.

## Automation in this repository

- `security.yml` runs CodeQL, `uv audit` against OSV, dependency review, and
  `detect-secrets` on every PR and weekly.
- Dependabot opens update PRs weekly (see `.github/dependabot.yml`); alerts must
  be enabled per
  [Repository administration](docs/Developer/Repository-Administration.md).
- Secrets scanning and private-vulnerability reporting are repository settings;
  the commands to enable them live in the same document.

Thank you for telling us quietly and giving people time to update.
