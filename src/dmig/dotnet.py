"""Bounded, logged subprocess runner for dotnet and git.

Full output always goes to a log file under the output directory; callers get the
exit code, duration and a short tail, so the agent context is never flooded.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .secrets import redact


@dataclass
class RunResult:
    command: list[str]
    cwd: str
    exit_code: int
    duration_s: float
    log_path: str | None
    output: str = field(repr=False, default="")
    timed_out: bool = False
    stdout: str = field(repr=False, default="")  # stdout alone (output = stdout + stderr)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def tail(self, lines: int = 15) -> str:
        return "\n".join(self.output.splitlines()[-lines:])

    def to_record(self) -> dict:
        return {
            "command": " ".join(self.command),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
            "timed_out": self.timed_out,
            "log": self.log_path,
        }


class Runner:
    """Runs commands with a fixed environment and writes every log to log_dir."""

    def __init__(self, dotnet: str | None = None, log_dir: Path | None = None):
        self.dotnet = dotnet or os.environ.get("DMIG_DOTNET") or shutil.which("dotnet") or "dotnet"
        self.log_dir = log_dir
        self._counter = 0
        self.deadline: float | None = None   # time budget for dotnet commands (set_budget)
        self.budget_exhausted = False
        self.env = dict(os.environ)
        self.env.update({
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_NOLOGO": "1",
            "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
            "MSBUILDDISABLENODEREUSE": "1",  # no lingering build servers locking files
            "DOTNET_CLI_UI_LANGUAGE": "en",
        })
        dotnet_dir = Path(self.dotnet).parent
        if self.dotnet != "dotnet" and (dotnet_dir / ("dotnet.exe" if os.name == "nt" else "dotnet")).exists():
            # A private dotnet root: make the runtime host and SDK resolve from it alone.
            self.env["DOTNET_ROOT"] = str(dotnet_dir)
            self.env["DOTNET_MULTILEVEL_LOOKUP"] = "0"
            self.env["PATH"] = str(dotnet_dir) + os.pathsep + self.env.get("PATH", "")

    def set_budget(self, minutes: float | None) -> None:
        """Limit the total time of dotnet commands (agent jobs have hard time limits). When the budget
        runs out, commands are not started or are stopped; the step then fails its gate and rolls back.
        git commands are exempt, so cleanup (removing worktrees) always runs."""
        self.deadline = time.monotonic() + minutes * 60 if minutes else None

    def run(self, args: list[str], cwd: Path, name: str, timeout_s: int = 1800,
            program: str | None = None) -> RunResult:
        cmd = [program or self.dotnet, *args]
        if self.deadline is not None and program is None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 5:
                self.budget_exhausted = True
                return RunResult(cmd, str(cwd), -1, 0.0, None, "time budget exhausted: command not started", True, "")
            timeout_s = min(timeout_s, int(remaining))
        start = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(cmd, cwd=str(cwd), env=self.env, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=timeout_s)
            output, code = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
            stdout = proc.stdout or ""
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stdout, code, timed_out = output, -1, True
        except FileNotFoundError as exc:
            output, code, stdout = f"command not found: {exc}", 127, ""
        duration = time.monotonic() - start
        if timed_out and self.deadline is not None and time.monotonic() >= self.deadline - 1:
            self.budget_exhausted = True
        # Combined output feeds logs, diagnostics and every JSON result: never let a credential through.
        # stdout stays raw because it carries patches (git diff), which are scanned separately.
        output = redact(output)
        log_path = None
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._counter += 1
            log_file = self.log_dir / f"{self._counter:03d}-{name}.log"
            log_file.write_text(redact(f"$ {' '.join(cmd)}\n# cwd: {cwd}\n# exit: {code}\n\n") + output,
                                encoding="utf-8")
            log_path = str(log_file)
        return RunResult(cmd, str(cwd), code, duration, log_path, output, timed_out, stdout)

    def git(self, args: list[str], cwd: Path, name: str = "git", timeout_s: int = 300) -> RunResult:
        return self.run(args, cwd, name, timeout_s, program="git")
