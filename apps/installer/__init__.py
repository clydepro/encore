"""The Installer: one command from a clean SD card to a running appliance (SAPRS 13, AIG 21 step 15).

Encore is two long-running programs in one repository — the Builder and the Server (ADR-001)
— and this is the third, which is neither: `encore-install` starts, does a list of things to
the machine it is standing on, reports what is still to do, and exits (ADR-014).

Six modules, split by *when the answer is known* rather than by noun:

* `layout.py` — where the appliance lives (SAPRS 13.3), as data, under one optional root.
* `system.py` — the only module that talks to the machine: `CommandRunner` for processes and
  `Host` for the filesystem, users, binaries and sockets.
* `prerequisites.py` — what has to be true first (13.1, 13.4, 13.5, 13.6), named and fixable.
* `plan.py` — the decision: observed state plus options, in, ordered actions out. Pure, and
  therefore the whole installer is testable without a root shell.
* `content.py` — what the rendered files say: `config.yaml`, the unit, the environment file.
* `execute.py`, `report.py`, `main.py` — the effects, the English, and the command line.

Two properties are the point of that split, and both are mandated rather than preferred.
**Idempotence** (13.7) is a property of the plan — running the installer twice produces a
second plan with nothing in it — and not of a script that happens to check before it acts.
**Non-destruction** (13.8) is a property of the actions: the config file has an action that
cannot overwrite, and `library.db`, `runtime.db`, the artwork and the logs are not in the
layout this installer manages at all, so "it preserved them" is structural rather than
promised.

What is deliberately not here: music. The installer builds no library and publishes none,
because the Builder needs a music directory that only the operator knows the location of
(ADR-010), and a program that scans 15,000 files as root during `apt`-style installation is
the wrong shape of surprise. It says so in the report instead, with the command.
"""

__all__: list[str] = []
