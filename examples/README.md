# examples/

Copyable, reviewed examples. Nothing here is read by the appliance.

- [`config.yaml`](config.yaml) — the shape of `/etc/encore/config.yaml`
  (installation-time configuration, SAPRS Chapter 12).

Rules:

- Examples are tested by being true: when a documented key changes, the example
  changes in the same PR, and the Configuration Service's validation tests cover
  both.
- No secrets, no real library paths, no hostnames that could belong to someone.
- Add an example per feature area as milestones land (builder invocation, systemd
  unit, backup schedule).
