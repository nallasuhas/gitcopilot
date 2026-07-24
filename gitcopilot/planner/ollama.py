"""Ollama-backed planner using structured JSON output.

Talks to a local Ollama daemon over its HTTP API (no third-party client -- just
``urllib`` from the stdlib, so the package has zero runtime dependencies). We
pass Ollama's ``format`` field the JSON schema from :mod:`planner.base`, which
constrains the model to emit exactly one valid :class:`ToolCall`. Malformed or
schema-violating replies raise :class:`PlannerError`; the agent retries a couple
of times before giving up.

The system prompt encodes the operating rules: prefer read-only inspection,
never chain destructive commands blindly, and stop (``done``) as soon as the
goal is answered. The *enforcement* of those rules is not trusted to the prompt
-- the classifier + permission gate are the real guardrail -- but a well-behaved
model makes the human confirmations rare and sensible.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .base import TOOL_SCHEMA, Planner, PlannerError, Step, ToolCall
from ..repostate import RepoState

_SYSTEM = """You are Git Copilot, a careful Git planning agent.

Your job is to achieve the user's Git goal by proposing EXACTLY ONE Git command at a time, observing its result, then deciding the next action.

You NEVER execute commands. You ONLY decide the next action.

Output Rules
------------
Respond ONLY with JSON matching the provided schema.

Valid actions:

{"action":"run_git","argv":[...],"rationale":"..."}
{"action":"done","summary":"..."}

- argv contains the Git command WITHOUT the leading "git".
- Propose exactly one Git command per turn.
- Never output prose outside the JSON.

Decision Process
----------------
For every turn:

1. Identify the user's actual goal.
2. Examine the repository state, previous commands, and their outputs.
3. Ask:
   - Is the user's goal already satisfied?
     -> Return {"action":"done"} immediately.
   - Otherwise, what is the SINGLE smallest Git command that moves the user closer to the goal?
4. Repeat until the goal is satisfied.

Never continue exploring once enough information has been obtained.

Planning Principles
-------------------
- Work incrementally.
- Each command should either:
  - answer one missing question, or
  - perform one explicitly requested Git operation.
- Every proposed command must move directly toward the user's goal.
- Do not broaden the user's request.
- Do not perform unrelated repository maintenance or convenience operations.
- Never repeat a command unless previous output clearly indicates it is necessary.

Repository State
----------------
Treat successful Git output as authoritative.

Use previous command output when planning.

Do not ignore successful output or speculate about repository state that Git has already reported.

Git Usage
---------
Use valid Git commands only.

Prefer the command whose primary purpose matches the user's request.

Examples:

- inspect repository -> status, diff, log, show
- inspect branches -> branch --list
- switch branch -> switch (or checkout)
- create branch -> switch -c (or checkout -b)
- delete branch -> branch -d / -D

Never invent flags or command combinations.

If uncertain, choose the simpler valid command instead of guessing.

Safety
------
Prefer read-only commands whenever possible.

Never convert an inspection request into a modification.

Do not modify the repository unless the user explicitly requested it.

Choose the least destructive command that accomplishes the goal.

Completion
----------
After every successful command ask:

'Has the user's original request now been satisfied?'

If yes, return:

{"action":"done","summary":"..."}

Do not perform extra steps that the user did not request.

Never invent commit messages, branch names, author names, or other missing user intent.

If the task cannot continue because required information is missing, return:

{"action":"done","summary":"..."}

The execution system is responsible for command classification, previews, and confirmation. Your responsibility is ONLY to choose the next correct Git action.
"""


class OllamaPlanner:
    name = "ollama"

    def __init__(self, model: str | None = None, host: str | None = None, timeout: float = 120.0):
        self.model = model or os.environ.get("GITCOPILOT_MODEL", "llama3.2")
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.timeout = timeout

    def next_action(
        self,
        goal: str,
        history: list[Step],
        state: RepoState | None = None,
    ) -> ToolCall:
        messages = [{"role": "system", "content": _SYSTEM}]
        if state is not None:
            messages.append({"role": "user", "content": _format_state(state)})
        messages.append({"role": "user", "content": f"Goal: {goal}"})
        for step in history:
            if step.executed:
                messages.append({
                    "role": "assistant",
                    "content": json.dumps({
                        "action": "run_git",
                        "argv": step.call.argv,
                        "rationale": step.call.rationale,
                    }),
                })
                messages.append({"role": "user", "content": f"Output:\n{step.output}"})
            elif step.denied_reason:
                messages.append({
                    "role": "user",
                    "content": (
                        f"The command `git {' '.join(step.call.argv or [])}` was NOT run: "
                        f"{step.denied_reason}. "
                        f"DO NOT propose this command again. "
                        f"Check the repository state: the user's goal may already be satisfied. "
                        f"If it is, return done. Otherwise propose a completely different approach."
                    ),
                })

        raw = self._chat(messages)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlannerError(f"model returned non-JSON: {raw[:200]!r}") from exc
        return ToolCall.from_json(obj)

    def _chat(self, messages: list[dict]) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": TOOL_SCHEMA,
            "options": {"temperature": 0.0},
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.URLError as exc:
            raise PlannerError(
                f"cannot reach Ollama at {self.host} ({exc}). "
                f"Is `ollama serve` running? Try `--offline` for a model-free demo.") from exc
        return body.get("message", {}).get("content", "")


def _format_state(state: RepoState) -> str:
    """Render a RepoState snapshot as a compact context message for the LLM."""
    if not state.is_repo:
        return "Current repository state: not a git repository."
    lines = ["Current repository state:"]
    if state.detached:
        lines.append(f"  branch: (detached HEAD at {state.head_short or 'unknown'})")
    else:
        lines.append(f"  branch: {state.branch or 'unknown'}")
    if state.upstream:
        lines.append(f"  upstream: {state.upstream} (ahead {state.ahead}, behind {state.behind})")
    else:
        lines.append("  upstream: (none)")
    parts = []
    if state.staged:
        parts.append(f"{state.staged} staged")
    if state.unstaged:
        parts.append(f"{state.unstaged} unstaged")
    if state.untracked:
        parts.append(f"{state.untracked} untracked")
    if state.unmerged:
        parts.append(f"{state.unmerged} conflicted")
    lines.append(f"  working tree: {', '.join(parts) if parts else 'clean'}")
    if state.in_progress:
        lines.append(f"  in progress: {state.in_progress}")
    if state.warnings:
        lines.append("  warnings:")
        for w in state.warnings:
            lines.append(f"    - {w}")
    return "\n".join(lines)


def build_planner(name: str, **kw) -> Planner:
    """Factory used by the CLI: 'offline' or 'ollama'."""
    if name == "ollama":
        return OllamaPlanner(**kw)
    from .offline import OfflinePlanner
    return OfflinePlanner()
