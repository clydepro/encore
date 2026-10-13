"""What the installer says when it is finished (SAPRS 13.7's last bullet).

"Report next steps" is the requirement this module exists to satisfy, and it is the one an
installer gets wrong most often — not by lying, but by succeeding. `encore-install install`
finishing with exit code zero on a machine with no library, no administrator and a hostname
that does not resolve is a *correct* report and a useless one, because the operator then closes
the laptop and the first person to find out otherwise is the one holding a phone in a hallway.

So the report has three parts and the third is the longest:

1. **What happened**, from `execute.py`'s transcript, including the actions that decided not
   to act — an idempotent installer's second run is indistinguishable from a silent one unless
   the no-ops are printed.
2. **What is true now**, from `checks.py`: 13.11's acceptance list, answered rather than
   asserted.
3. **What to do next**, in the order that gets a party going, each line a command that can be
   pasted. These are derived from the same `EncoreConfig` the installer wrote, so the URL in
   step three is the URL the QR will encode.

`--json` is the same three sections as data, for a provisioning system that has to know whether
"exit 0" meant "installed" or "already installed".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

from apps.installer.checks import Check, Verdict
from apps.installer.execute import Execution
from apps.installer.options import Command, Options
from apps.installer.plan import Action, Plan

if TYPE_CHECKING:
    from encore.config.models import EncoreConfig

__all__ = ["Report", "next_steps"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Report:
    """One finished invocation.

    Attributes:
        options: What was asked for, because a report that does not name its own command is
            two lines into being ambiguous.
        plan: What was decided. An empty plan is a result, and is printed as one.
        execution: What happened. Empty for a plan, a verify, or a dry run of either.
        checks: What is true now, when the command asked for that question.
        notes: Anything the plan decided that is not an action — a preserved configuration, a
            group that does not exist, an account left behind.
        exit_code: Already resolved, so `main.py` is not the only reader of the rules.
    """

    options: Options
    plan: Plan
    execution: Execution = field(default_factory=Execution)
    checks: tuple[Check, ...] = ()
    notes: tuple[str, ...] = ()
    exit_code: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def did_nothing(self) -> bool:
        """Whether the machine already matched the request, which 13.7 calls success."""

        return self.plan.is_empty or self.execution.changed == 0

    def text(self, *, config: EncoreConfig) -> str:
        lines = [f"encore-install {self.options.command.value}"]
        if self.options.staged:
            lines.append(
                f"  staged under {self.options.root} — this appliance lives inside that prefix"
            )
        lines.append("")

        if self.plan.is_empty:
            lines.append(self.plan.describe())
        else:
            lines.append("Plan:")
            lines.append(self.plan.describe())
        lines.append("")

        if self.execution.results:
            lines.append("Result:")
            lines.extend(result.line() for result in self.execution.results)
            lines.append("")
            lines.append(
                f"{self.execution.changed} action(s) performed, "
                f"{self.execution.noops} found already done."
            )
            lines.append("")

        if self.checks:
            failures = sum(1 for check in self.checks if check.verdict is Verdict.FAIL)
            warnings = sum(1 for check in self.checks if check.verdict is Verdict.WARN)
            lines.append("Acceptance (SAPRS 13.11):")
            lines.extend(check.line() for check in self.checks)
            lines.append("")
            lines.append(
                f"{len(self.checks) - failures - warnings} ok, {warnings} warning(s), "
                f"{failures} failure(s)."
            )
            lines.append("")

        lines.extend(f"  note  {note}" for note in self.notes)
        if self.notes:
            lines.append("")

        steps = next_steps(self.options, config)
        if steps:
            lines.append("Next:")
            for index, step in enumerate(steps, 1):
                lines.append(f"  {index}. {step[0]}")
                if step[1]:
                    lines.append(f"        {step[1]}")
            lines.append("")

        lines.append(_verdict(self))
        return "\n".join(lines)

    def json(self, *, config: EncoreConfig) -> str:
        """The same report as data. See the module docstring for who reads it."""

        payload: dict[str, Any] = {
            "command": self.options.command.value,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "root": str(self.options.root),
            "staged": self.options.staged,
            "dry_run": self.options.dry_run,
            "plan": [_plan_entry(action) for action in self.plan.actions],
            "performed": self.execution.changed,
            "already_done": self.execution.noops,
            "results": [
                {
                    "kind": result.action.kind,
                    "detail": result.action.detail,
                    "status": result.status.value,
                    "output": result.detail,
                    "finding": result.finding,
                }
                for result in self.execution.results
            ],
            "checks": [
                {
                    "name": check.name,
                    "verdict": check.verdict.value,
                    "detail": check.detail,
                    "fix": check.fix,
                }
                for check in self.checks
            ],
            "notes": list(self.notes),
            "next_steps": [
                {"step": step[0], "command": step[1]} for step in next_steps(self.options, config)
            ],
            "guest_url": config.server.guest_url,
        }
        return json.dumps(payload, indent=2, sort_keys=False)


def _plan_entry(action: Action) -> dict[str, Any]:
    """One planned action, as data.

    `detail` is the sentence; the extras are the evidence behind it, included when the action
    carries any. A `PreserveConfig` whose differences appeared only in the text report would make
    `--dry-run --json` the one way to *not* find out what a run would decline to change, and the
    reason 13.8's report exists is for a provisioning system that never reads prose.

    Deliberately a small allowlist rather than a `vars()` dump: `EnsureFile` holds a file's whole
    contents, and `CreateServiceUser` holds a password. The environment file's text in a JSON
    report is a secret in a log.
    """

    entry: dict[str, Any] = {"kind": action.kind, "detail": action.detail}
    path: object = getattr(action, "path", None)
    if isinstance(path, Path):
        entry["path"] = str(path)
    differences = getattr(action, "differences", ())
    if differences:
        entry["differences"] = [
            {"setting": str(name), "on_disk": str(here), "asked_for": str(there)}
            for name, here, there in cast("Sequence[tuple[object, object, object]]", differences)
        ]
    problems = getattr(action, "problems", ())
    if problems:
        entry["problems"] = [str(problem) for problem in cast("Sequence[object]", problems)]
    return entry


def next_steps(options: Options, config: EncoreConfig) -> tuple[tuple[str, str], ...]:
    """The list an operator acts on, in the order the actions depend on each other.

    Built from the configuration the installer wrote rather than from the flags it was given, so
    a preserved `config.yaml` (13.8) produces steps that describe the appliance that is actually
    running instead of the one somebody's command line asked for.

    The command comes from `options` rather than from a parameter. It was a parameter, and the
    report had to remember to pass its own copy — which is the shape of bug where a removal
    prints "open the guest interface" about a machine it just deleted.
    """

    command = options.command

    url = config.server.guest_url
    venv = options.layout.staged(options.layout.venv)
    steps: list[tuple[str, str]] = []

    if command is Command.REMOVE:
        steps.append(
            (
                "The appliance is gone. The music, the databases and the configuration were left "
                "where they were, unless this command was asked to purge them (SAPRS 13.10).",
                f"ls -d {options.layout.staged(options.layout.state)} "
                f"{options.layout.staged('/etc/encore')}",
            )
        )
        steps.append(
            (
                "The service account is still there. Deleting it is a decision rather than a "
                "cleanup step: it may own things this installer never touched.",
                f"sudo userdel -r {options.layout.user}  # only if you created it for Encore",
            )
        )
        return tuple(steps)

    if command is Command.RESET_CREDENTIAL:
        steps.append(("Sign in at " + url + "/admin and set a new password.", ""))
        steps.append(("Start it again if it was stopped.", "sudo systemctl start encore"))
        return tuple(steps)

    steps.append(
        (
            f"Open the guest interface: {url}",
            f"curl -fsS {url}/api/v1/health",
        )
    )
    steps.append(
        (
            "Build the library on the machine that holds the music, then publish it here "
            "(ADR-010). The Server never scans a music directory.",
            f"{venv}/bin/encore-builder --music-dir {config.paths.music_dir}",
        )
    )
    steps.append(
        (
            "Set the administrator password once at " + url + "/admin, or seed it on first boot "
            "with ENCORE_ADMIN_PASSWORD in "
            + str(options.layout.staged(options.layout.environment_file))
            + ".",
            "",
        )
    )
    steps.append(("Print the QR code for the wall.", f"{url}/qr"))
    steps.append(
        (
            "Check the acceptance list at any time.",
            "sudo encore-install verify",
        )
    )
    if not options.service:
        steps.append(
            (
                "No systemd unit was installed. Start it by hand, or re-run without --no-service.",
                "scripts/run-server.sh",
            )
        )
    return tuple(steps)


def _verdict(report: Report) -> str:
    if not report.ok:
        return "Failed. The fix is in the lines above; nothing after the failure was attempted."
    if report.options.dry_run:
        return "Nothing changed (--dry-run)."
    if report.did_nothing and report.options.command is Command.INSTALL:
        return "Already installed — every step found the machine in the state it wanted (13.7)."
    if report.options.command is Command.VERIFY:
        return "Verified."
    return "Done."
