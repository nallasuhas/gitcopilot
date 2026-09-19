"""The observe -> decide -> act loop, git-aware.

This is the same core loop Claude Code runs, specialized for git:

    1. OBSERVE   snapshot repo state (read-only) so we never act on a surprise.
    2. DECIDE    ask the planner for ONE next tool call.
    3. GATE      classify the proposed command:
                    read-only    -> run immediately
                    mutating     -> ask the human to confirm
                    destructive  -> build a preview, then ask to confirm
    4. ACT       run it (or record the denial), feed the output back.
    5. repeat until the planner says `done`, a step budget is hit, or the human
       declines and the planner has nothing else to try.

The safety decisions live here and in :mod:`classifier`/:mod:`preview`, NOT in
the planner's prompt -- so even a misbehaving or adversarial model cannot run a
destructive command without a human seeing the blast radius first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .classifier import Classification, Tier, classify
from .gitcmd import GitResult, GitRunner
from .planner.base import Planner, PlannerError, Step, ToolCall
from .preview import Preview, build_preview
from .repostate import RepoState, snapshot
from .validator import validate


@dataclass
class Decision:
    """What the human (or policy) decided about a gated command."""

    approved: bool
    reason: str = ""


# A confirmer receives the proposed call, its classification, and an optional
# preview, and returns a Decision. The CLI supplies an interactive one; tests
# and `--yes`/`--explain` modes supply automatic ones.
Confirmer = Callable[[ToolCall, Classification, Preview | None, RepoState], Decision]


@dataclass
class AgentConfig:
    max_steps: int = 12          # step budget: halt a runaway loop
    explain_only: bool = False   # "explain, don't run" learning mode
    auto_read_only: bool = True  # auto-run read-only commands without a prompt
    retries: int = 2             # planner retries on malformed tool calls


@dataclass
class LoopResult:
    goal: str
    steps: list[Step] = field(default_factory=list)
    summary: str = ""
    halted_reason: str = ""      # "" == finished normally via `done`


class Agent:
    def __init__(
        self,
        planner: Planner,
        runner: GitRunner,
        confirm: Confirmer,
        config: AgentConfig | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ):
        self.planner = planner
        self.runner = runner
        self.confirm = confirm
        self.config = config or AgentConfig()
        self.on_event = on_event or (lambda kind, data: None)

    def run(self, goal: str) -> LoopResult:
        result = LoopResult(goal=goal)
        # Track denied commands so we can auto-decline repeats without pestering
        # the user, and detect a planner that's stuck in a denial loop.
        denied_argv: set[tuple[str, ...]] = set()
        consecutive_denials = 0
        max_consecutive_denials = 2  # halt if the planner keeps re-proposing denied cmds

        for _ in range(self.config.max_steps):
            state = snapshot(self.runner)
            self.on_event("state", {"state": state})

            call = self._decide(goal, result.steps, state)
            if call is None:
                result.halted_reason = "planner produced no valid action"
                break

            if call.action == "done":
                result.summary = call.summary
                self.on_event("done", {"summary": call.summary})
                break

            cls = classify(call.argv or [])
            step = Step(call=call, tier=cls.tier.label)
            self.on_event("propose", {"call": call, "cls": cls, "state": state})

            # Guardrail: if the planner re-proposes a command the user already
            # denied, auto-decline it without prompting again. This prevents a
            # model from pestering the user with the same gated command.
            argv_key = tuple(call.argv or [])
            if argv_key in denied_argv:
                step.executed = False
                step.denied_reason = "auto-skipped: this command was already declined by the user"
                result.steps.append(step)
                self.on_event("denied", {"call": call, "reason": step.denied_reason})
                consecutive_denials += 1
                if consecutive_denials >= max_consecutive_denials:
                    result.halted_reason = (
                        "planner is stuck re-proposing denied commands; "
                        "the goal may already be satisfied or may need a different approach"
                    )
                    break
                continue

            decision = self._gate(call, cls, state)
            if not decision.approved:
                step.executed = False
                step.denied_reason = decision.reason or "declined by user"
                result.steps.append(step)
                self.on_event("denied", {"call": call, "reason": step.denied_reason})
                denied_argv.add(argv_key)
                # A new denial (different command) resets the repeat-streak;
                # the planner tried something new, it just didn't get approved.
                consecutive_denials = 0
                continue

            # A command was approved and will run — reset the denial streak.
            consecutive_denials = 0

            res = self.runner.run(call.argv or [])
            step.executed = True
            step.output = res.summary()
            result.steps.append(step)
            self.on_event("ran", {"call": call, "result": res})

        else:
            result.halted_reason = f"reached step budget ({self.config.max_steps})"

        if not result.summary and not result.halted_reason:
            result.halted_reason = result.halted_reason or ""
        return result

    # --- internals -----------------------------------------------------------

    def _decide(self, goal: str, history: list[Step], state: RepoState | None = None) -> ToolCall | None:
        last_err = None
        for _ in range(self.config.retries + 1):
            try:
                call = self.planner.next_action(goal, history, state)
                # Validate the proposed command against real repo state before
                # it reaches the classifier/gate. A validation failure is a
                # PlannerError the loop retries — same as a malformed JSON reply.
                if call.action == "run_git" and state is not None:
                    call = validate(call, self.runner, state)
                return call
            except PlannerError as exc:
                last_err = exc
                self.on_event("planner_error", {"error": str(exc)})
        self.on_event("planner_failed", {"error": str(last_err)})
        return None

    def _gate(self, call: ToolCall, cls: Classification, state: RepoState) -> Decision:
        # Learning mode: never run anything, just explain.
        if self.config.explain_only:
            return Decision(False, "explain-only mode: command not executed")

        if cls.tier == Tier.READ_ONLY and self.config.auto_read_only:
            return Decision(True, "auto-approved (read-only)")

        preview: Preview | None = None
        if cls.tier == Tier.DESTRUCTIVE:
            preview = build_preview(call.argv or [], self.runner)

        return self.confirm(call, cls, preview, state)


# --- ready-made confirmers ---------------------------------------------------

def always_yes(call: ToolCall, cls: Classification, preview: Preview | None, state: RepoState) -> Decision:
    """Approve everything -- for tests and non-interactive `--yes` runs.

    Note: this deliberately ignores the tier. Use only where the caller has
    accepted the risk (a scratch repo, CI). The CLI never wires this to a real
    repo without an explicit flag.
    """
    return Decision(True, "auto-approved (--yes)")


def always_no(call: ToolCall, cls: Classification, preview: Preview | None, state: RepoState) -> Decision:
    """Decline every gated command -- a pure dry-run of the loop."""
    return Decision(False, "dry-run: all gated commands declined")
