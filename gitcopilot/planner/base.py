"""Planner protocol + the structured tool-call contract.

The agent never parses free-form model prose. Instead, every planner returns a
single validated :class:`ToolCall` -- the model's *one next action*. There are
exactly two actions:

    run_git   -- run this git command (argv given), for this stated reason.
    done      -- the goal is achieved (or impossible); here is the summary.

Constraining the model to this tiny JSON schema is what makes the loop robust:
a malformed reply is a validation error we can retry, not a mystery string we
have to interpret. The classifier and permission gate then decide whether the
proposed `run_git` actually executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..repostate import RepoState


class PlannerError(Exception):
    """Raised when a planner cannot produce a valid tool call."""


@dataclass(frozen=True)
class ToolCall:
    action: Literal["run_git", "done"]
    #: For run_git: the git argv WITHOUT a leading "git" (e.g. ["status","-s"]).
    argv: list[str] | None = None
    #: One sentence: why this command advances the goal.
    rationale: str = ""
    #: For done: the plain-English answer/summary for the user.
    summary: str = ""

    @staticmethod
    def from_json(obj: dict) -> "ToolCall":
        action = obj.get("action")
        if action not in ("run_git", "done"):
            raise PlannerError(f"invalid action: {action!r}")
        if action == "run_git":
            argv = obj.get("argv")
            if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
                raise PlannerError("run_git requires argv: list[str]")
            if argv and argv[0] == "git":
                argv = argv[1:]
            if not argv:
                raise PlannerError("run_git argv is empty")
            return ToolCall("run_git", argv=argv, rationale=str(obj.get("rationale", "")))
        return ToolCall("done", summary=str(obj.get("summary", "")))


@dataclass
class Step:
    """One turn of the observed loop, recorded for the transcript."""

    call: ToolCall
    tier: str | None = None
    executed: bool = False
    output: str = ""
    denied_reason: str = ""


# The JSON schema we advertise to structured-output-capable backends.
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["run_git", "done"]},
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "description": "git command as an argument list, no leading 'git'",
        },
        "rationale": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["action"],
}


@runtime_checkable
class Planner(Protocol):
    """Anything that can propose the next action given the goal + history.

    The optional ``state`` argument carries the current :class:`RepoState`
    snapshot so the planner can ground its proposals in the actual repo
    context (current branch, upstream, dirty state, warnings). Planners that
    don't need it may accept and ignore it.
    """

    name: str

    def next_action(
        self,
        goal: str,
        history: list[Step],
        state: "RepoState | None" = None,
    ) -> ToolCall:
        ...
