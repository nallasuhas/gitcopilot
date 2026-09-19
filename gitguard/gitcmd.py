"""Thin, safe wrapper around invoking the real `git` binary.

We never build a shell string -- commands are always an argv list passed to
``subprocess.run`` with ``shell=False``, so there is no shell-injection surface
even though the arguments originate from a language model. A wall-clock timeout
bounds every call so a pager or a prompt can't hang the agent.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class GitResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def summary(self, max_chars: int = 4000) -> str:
        """Compact stdout+stderr for feeding back to the model."""
        body = self.stdout
        if self.stderr.strip():
            body = f"{body}\n[stderr]\n{self.stderr}" if body.strip() else self.stderr
        body = body.strip()
        if len(body) > max_chars:
            body = body[:max_chars] + f"\n… ({len(body) - max_chars} more chars truncated)"
        head = f"$ git {' '.join(self.argv)}\n(exit {self.returncode}"
        head += ", timed out)" if self.timed_out else ")"
        return f"{head}\n{body}" if body else head


class GitRunner:
    """Runs git in a fixed working directory.

    Set ``cwd`` to a scratch clone for demos/CI so nothing touches real work.
    """

    def __init__(self, cwd: str | None = None, timeout: float = 30.0):
        self.cwd = cwd or os.getcwd()
        self.timeout = timeout

    def run(self, argv: list[str]) -> GitResult:
        # Strip a leading "git" if the caller included it; we add our own.
        args = list(argv)
        if args and args[0] == "git":
            args = args[1:]
        # `-c core.pager=cat` and friends keep git from paging or prompting,
        # which would otherwise block a non-interactive subprocess.
        full = [
            "git",
            "-c", "core.pager=cat",
            "-c", "color.ui=never",
            *args,
        ]
        env = dict(os.environ)
        env["GIT_PAGER"] = "cat"
        env["GIT_TERMINAL_PROMPT"] = "0"  # never block on a credential prompt
        try:
            proc = subprocess.run(
                full,
                cwd=self.cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return GitResult(
                argv=args,
                returncode=124,
                stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                stderr=exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
                timed_out=True,
            )
        except FileNotFoundError:
            return GitResult(args, 127, "", "git executable not found on PATH")
        return GitResult(args, proc.returncode, proc.stdout, proc.stderr)

    def is_repo(self) -> bool:
        return self.run(["rev-parse", "--is-inside-work-tree"]).stdout.strip() == "true"
