"""The deterministic planner maps intents to command sequences."""

from gitguard.planner.offline import OfflinePlanner
from gitguard.planner.base import Step, ToolCall


def first_call(goal):
    return OfflinePlanner().next_action(goal, [])


def test_history_intent():
    call = first_call("show me the recent history")
    assert call.action == "run_git"
    assert call.argv[0] == "log"


def test_status_intent():
    call = first_call("what's the current status?")
    assert call.argv[0] == "status"


def test_undo_last_commit_keep_changes():
    call = first_call("undo my last commit but keep the changes")
    assert call.argv == ["reset", "--soft", "HEAD~1"]


def test_undo_last_commit_hard():
    call = first_call("undo the last commit and discard the changes")
    assert call.argv == ["reset", "--hard", "HEAD~1"]


def test_delete_branch_extracts_name():
    call = first_call("delete branch old-experiment")
    assert call.argv[0] == "branch"
    # First step confirms current branch; the delete comes next.
    step = Step(call=call, executed=True, output="main")
    nxt = OfflinePlanner().next_action("delete branch old-experiment", [step])
    assert nxt.argv[:2] == ["branch", "-d"]
    assert "old-experiment" in nxt.argv


def test_force_push_prefers_lease():
    goal = "force push my changes"
    p = OfflinePlanner()
    step = Step(call=p.next_action(goal, []), executed=True, output="ok")
    nxt = p.next_action(goal, [step])
    assert nxt.argv == ["push", "--force-with-lease"]


def test_plan_terminates_with_done():
    p = OfflinePlanner()
    goal = "show me the recent history"
    history = [Step(call=p.next_action(goal, []), executed=True, output="…")]
    nxt = p.next_action(goal, history)
    assert nxt.action == "done"
    assert nxt.summary


def test_stage_all_two_steps():
    p = OfflinePlanner()
    goal = "stage everything"
    c1 = p.next_action(goal, [])
    assert c1.argv == ["add", "-A"]


def test_switch_to_branch_extracts_name():
    call = first_call("switch to the branch feature")
    assert call.action == "run_git"
    assert call.argv == ["switch", "feature"]


def test_checkout_branch_extracts_name():
    call = first_call("checkout branch main")
    assert call.action == "run_git"
    assert call.argv == ["switch", "main"]


def test_switch_branch_without_name_lists_branches():
    call = first_call("switch to a branch")
    assert call.action == "run_git"
    assert call.argv[0] == "branch"
