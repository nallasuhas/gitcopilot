"""End-to-end loop tests: planner + classifier + gate + real git.

These prove the guardrails hold regardless of what the planner proposes:
read-only auto-runs, gated commands only run when the confirmer approves, the
step budget halts runaway loops, and the preview reflects real repo state.
"""

from gitguard.agent import Agent, AgentConfig, Decision, always_no, always_yes
from gitguard.classifier import Tier
from gitguard.gitcmd import GitRunner
from gitguard.planner.base import Planner, Step, ToolCall
from gitguard.preview import build_preview
from gitguard.repostate import snapshot


class ScriptedPlanner:
    """A planner that emits a fixed list of tool calls, then `done`."""
    name = "scripted"

    def __init__(self, calls):
        self._calls = calls

    def next_action(self, goal, history, state=None):
        i = len(history)
        if i < len(self._calls):
            return self._calls[i]
        return ToolCall("done", summary=f"ran {i} step(s)")


def test_read_only_auto_runs(runner):
    planner = ScriptedPlanner([ToolCall("run_git", argv=["status", "--short"], rationale="check")])
    # Confirmer would DENY, but read-only should never reach it.
    agent = Agent(planner, runner, always_no, AgentConfig())
    result = agent.run("look around")
    assert result.steps[0].executed is True
    assert result.steps[0].tier == "read-only"


def test_mutating_requires_confirmation(runner):
    planner = ScriptedPlanner([ToolCall("run_git", argv=["add", "-A"], rationale="stage")])
    denied = Agent(planner, runner, always_no, AgentConfig())
    r1 = denied.run("stage")
    assert r1.steps[0].executed is False
    assert "declined" in r1.steps[0].denied_reason

    approved = Agent(planner, runner, always_yes, AgentConfig())
    r2 = approved.run("stage")
    assert r2.steps[0].executed is True


def test_destructive_gets_preview_passed_to_confirmer(runner):
    seen = {}

    def capture(call, cls, preview, state):
        seen["tier"] = cls.tier
        seen["preview"] = preview
        return Decision(False, "no")

    planner = ScriptedPlanner([ToolCall("run_git", argv=["reset", "--hard", "HEAD~1"], rationale="undo")])
    Agent(planner, runner, capture, AgentConfig()).run("nuke last commit")
    assert seen["tier"] == Tier.DESTRUCTIVE
    assert seen["preview"] is not None
    # The preview should mention the commit that would be dropped.
    text = seen["preview"].render()
    assert "commit" in text.lower()


def test_explain_mode_runs_nothing(runner):
    planner = ScriptedPlanner([
        ToolCall("run_git", argv=["status"], rationale="a"),
        ToolCall("run_git", argv=["add", "-A"], rationale="b"),
    ])
    agent = Agent(planner, runner, always_yes, AgentConfig(explain_only=True))
    result = agent.run("do stuff")
    assert all(not s.executed for s in result.steps)


def test_step_budget_halts(runner):
    # A planner that never says done.
    class Infinite:
        name = "inf"
        def next_action(self, goal, history, state=None):
            return ToolCall("run_git", argv=["status"], rationale="again")

    agent = Agent(Infinite(), runner, always_yes, AgentConfig(max_steps=3))
    result = agent.run("loop forever")
    assert len(result.steps) == 3
    assert "budget" in result.halted_reason


def test_denial_is_fed_back_so_planner_can_pivot(runner):
    # Planner proposes a mutating command; when denied, it should get a chance
    # to propose again (here it just finishes).
    calls = [ToolCall("run_git", argv=["commit", "-m", "x"], rationale="commit")]
    agent = Agent(ScriptedPlanner(calls), runner, always_no, AgentConfig())
    result = agent.run("commit")
    # One denied step, then done.
    assert result.steps[0].executed is False
    assert result.summary  # planner produced a done summary afterwards


def test_full_offline_flow_on_real_repo(runner):
    from gitguard.planner.offline import OfflinePlanner
    agent = Agent(OfflinePlanner(), runner, always_yes, AgentConfig(max_steps=6))
    result = agent.run("what changed in my working tree?")
    executed = [s for s in result.steps if s.executed]
    assert executed
    assert executed[0].call.argv[0] == "status"
    assert result.summary


def test_repeated_denied_command_halts_loop(runner):
    """A planner that keeps re-proposing a denied command should be halted,
    not pester the user indefinitely."""
    # This planner always proposes the same mutating command, never done.
    class Stubborn:
        name = "stubborn"
        def next_action(self, goal, history, state=None):
            return ToolCall("run_git", argv=["commit", "-m", "x"], rationale="try again")

    agent = Agent(Stubborn(), runner, always_no, AgentConfig(max_steps=10))
    result = agent.run("commit something")
    # The loop should halt (not hit the step budget of 10).
    assert result.halted_reason
    assert "stuck" in result.halted_reason.lower() or "denied" in result.halted_reason.lower()
    # The user should only have been prompted once (the first denial);
    # subsequent identical proposals are auto-skipped.
    user_prompted = [s for s in result.steps if "auto-skipped" not in (s.denied_reason or "")]
    auto_skipped = [s for s in result.steps if "auto-skipped" in (s.denied_reason or "")]
    assert len(user_prompted) == 1  # only the first one reached the confirmer
    assert len(auto_skipped) >= 1   # at least one repeat was auto-skipped


def test_denied_then_different_command_continues(runner):
    """If the planner proposes a different command after a denial, the loop
    should continue normally (not halt on 'stuck')."""
    calls = [
        ToolCall("run_git", argv=["commit", "-m", "x"], rationale="first try"),
        ToolCall("run_git", argv=["add", "-A"], rationale="different approach"),
    ]
    agent = Agent(ScriptedPlanner(calls), runner, always_no, AgentConfig())
    result = agent.run("commit")
    # First command denied, second command also denied (always_no), but they're
    # different so the loop shouldn't halt on "stuck" — it should get done
    # after the planner exhausts its plan.
    assert not result.halted_reason or "stuck" not in result.halted_reason.lower()
    assert result.summary  # planner said done
