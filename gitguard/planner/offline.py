"""Deterministic, model-free planner.

Why this exists: demos and CI must run with no Ollama daemon and no network,
and the safety machinery (classifier, permission gate, previews, transcript)
should be testable without a stochastic model in the loop. This planner maps
common natural-language git goals to a fixed sequence of git commands using
keyword rules -- the same "scripted planner" pattern used in the sibling
rag-chat project's offline backend.

It is intentionally simple, not clever. It recognizes the handful of intents a
demo needs ("what changed?", "show history", "undo last commit", "delete this
branch", "discard my changes") and otherwise falls back to `git status` and
then declares done. The point is a predictable, reproducible loop -- the real
intelligence is the Ollama planner.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .base import Planner, Step, ToolCall

if TYPE_CHECKING:
    from ..repostate import RepoState


class OfflinePlanner:
    name = "offline"

    def next_action(
        self,
        goal: str,
        history: list[Step],
        state: "RepoState | None" = None,
    ) -> ToolCall:
        plan = _plan_for(goal)
        # We've already executed `len(history)` steps; emit the next command in
        # the plan, or declare done when the plan is exhausted.
        idx = len(history)
        if idx < len(plan):
            argv, why = plan[idx]
            return ToolCall("run_git", argv=argv, rationale=why)
        return ToolCall("done", summary=_summarize(goal, history))


def _plan_for(goal: str) -> list[tuple[list[str], str]]:
    g = goal.lower().strip()

    def has(*words: str) -> bool:
        return any(re.search(rf"\b{re.escape(w)}\b", g) for w in words)

    # Order matters: most specific intents first.
    if has("undo", "revert") and has("last", "latest") and has("commit"):
        if has("keep", "soft", "unstage"):
            return [(["reset", "--soft", "HEAD~1"], "move HEAD back one commit, keeping changes staged")]
        if has("hard", "discard", "throw away"):
            return [(["reset", "--hard", "HEAD~1"], "reset to the previous commit, discarding the last commit's changes")]
        return [(["reset", "--soft", "HEAD~1"], "undo the last commit but keep its changes (safe default)")]

    if has("discard", "throw away", "reset") and has("changes", "edits", "working", "modifications"):
        return [
            (["status", "--short"], "see what would be discarded first"),
            (["checkout", "--", "."], "discard all uncommitted changes in tracked files"),
        ]

    if has("delete", "remove") and has("branch"):
        name = _extract_branch(goal)
        target = name or "<branch>"
        return [
            (["branch", "--show-current"], "confirm we're not on the branch we're deleting"),
            (["branch", "-d", target], f"delete branch {target} (safe delete; won't drop unmerged work)"),
        ]

    if has("force") and has("push"):
        return [
            (["status", "--short", "--branch"], "check ahead/behind before rewriting the remote"),
            (["push", "--force-with-lease"], "publish rewritten history without clobbering others' work"),
        ]

    if has("stage", "add") and has("all", "everything"):
        return [
            (["add", "-A"], "stage all changes"),
            (["status", "--short"], "confirm what got staged"),
        ]

    if has("commit"):
        return [(["status", "--short"], "review what would be committed (offline planner won't invent a message)")]

    if has("history", "log", "commits", "recent"):
        return [(["log", "--oneline", "--graph", "--decorate", "-n", "15"],
                 "show a compact graph of recent history")]

    # Switch to an existing branch. Must come before the generic branch-listing
    # rule so "switch to the branch feature" doesn't fall through to `branch -vv`.
    if has("switch", "checkout", "go to", "change") and has("branch"):
        name = _extract_branch(goal)
        if name:
            return [(["switch", name], f"switch to branch {name}")]
        return [(["branch", "--list"], "list branches so the user can pick one")]

    if has("branch", "branches") and not has("delete", "remove"):
        return [(["branch", "-vv"], "list branches with their upstreams and last commit")]

    if has("diff", "changed", "changes", "modified", "what did i"):
        return [
            (["status", "--short"], "list changed files"),
            (["diff", "--stat"], "summarize the size of the changes"),
        ]

    if has("stash"):
        return [(["stash", "list"], "show the current stash entries")]

    if has("status", "state", "where am i", "clean"):
        return [(["status", "--short", "--branch"], "show working-tree and branch status")]

    # Fallback: observe, then finish.
    return [(["status", "--short", "--branch"], "no specific intent recognized; inspect repo state")]


def _extract_branch(goal: str) -> str | None:
    # Grab a token after "branch" that looks like a ref name.
    m = re.search(r"branch\s+['\"]?([\w./-]+)['\"]?", goal, re.I)
    if m and m.group(1).lower() not in {"the", "a", "this", "my", "named", "called"}:
        return m.group(1)
    m = re.search(r"['\"]([\w./-]+)['\"]", goal)
    return m.group(1) if m else None


def _summarize(goal: str, history: list[Step]) -> str:
    ran = [s for s in history if s.executed]
    if not ran:
        return f"Nothing was executed for: {goal!r}. Review the proposed commands above."
    last = ran[-1]
    return (f"Ran {len(ran)} command(s) toward: {goal!r}. "
            f"Last: `git {' '.join(last.call.argv or [])}`.")
