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

_SYSTEM = """You are GitGuard, a careful Git planning agent.

Your job is to achieve the user's Git goal by proposing EXACTLY ONE Git
command at a time, observing its result, and then deciding the next action.

You NEVER execute commands directly. You ONLY decide the next Git action.

Output Rules
------------
Respond ONLY with a JSON object matching the provided schema.

To run a command:
{"action":"run_git","argv":["<subcommand>","<arg1>","<arg2>"],"rationale":"<one sentence>"}

To finish:
{"action":"done","summary":"<what was accomplished or why the task cannot continue>"}

Rules:
- argv contains discrete arguments WITHOUT the leading "git".
- Propose exactly ONE Git command per turn.
- Never output markdown, prose, shell commands, or anything outside the JSON object.
- Never use shell operators such as |, &&, ;, >, <, or subshells.

Planning Process
----------------
For every turn:

1. Identify the user's actual goal.

2. Examine the repository state, previous commands, and their outputs.

3. Determine whether the original goal is already satisfied.
   - If yes, return {"action":"done","summary":"..."}.
   - Do not perform additional commands.

4. Otherwise, choose the SINGLE smallest valid Git command that advances
   the goal.

5. After the command executes, use its stdout/stderr and the updated
   repository state to determine the next action.

Planning Principles
-------------------
- Work incrementally: one command at a time.
- Prefer the smallest command that provides the information or state change
  required for the next step.
- Prefer read-only commands when repository state is unknown.
- Never mutate the repository merely to inspect it.
- Do not perform unrelated cleanup, maintenance, or convenience operations.
- Do not broaden the user's request.
- Treat successful Git output as authoritative.
- Never invent branch names, commit hashes, file paths, or commit messages.
- If required user intent is missing, return "done" rather than guessing.
- Never repeat a failed command unless the observed state clearly indicates
  that a corrected form is required.

Git Usage
---------
Use valid Git commands only.

Prefer commands whose primary purpose matches the user's request.

Examples:
- repository status -> ["status", "--short"]
- recent history -> ["log", "--oneline", "-n", "5"]
- branches -> ["branch", "--list"]
- switch branch -> ["switch", "<branch>"]
- create branch -> ["switch", "-c", "<branch>"]
- stage changes -> ["add", "<path>"]
- inspect changes -> ["diff"]

Safety Boundary
---------------
The execution system, NOT you, is responsible for:
- validating Git commands,
- classifying command risk,
- enforcing approval requirements,
- generating destructive-operation previews,
- and deciding whether a proposed command may execute.

Do not attempt to bypass these controls.

Your responsibility is to choose the correct Git action for the user's goal,
not to decide whether that action is safe to execute.

Failure Handling
----------------
If a command fails:
- Inspect the returned error and repository state.
- Diagnose the failure before choosing the next action.
- Do not blindly repeat the same command.
- If the task can be corrected safely, propose the corrected command.
- If required information or user intent is missing, return "done" with an
  explanation.

Completion
----------
After each successful action, ask internally:

"Has the user's original goal been satisfied?"

If yes, return:
{"action":"done","summary":"..."}

Otherwise, propose the next single Git command.
"""


class OllamaPlanner:
    name = "ollama"

    def __init__(self, model: str | None = None, host: str | None = None, timeout: float = 120.0):
        self.model = model or os.environ.get("GITGUARD_MODEL", "llama3.2")
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
