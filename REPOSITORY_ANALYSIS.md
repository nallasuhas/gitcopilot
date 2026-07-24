# Git Copilot — Repository Analysis Report

> A thorough, code-grounded analysis of the `git-copilot-main` repository.
> Every claim below is backed by a specific file/function reference. Where the
> code cannot answer a question, that is stated explicitly rather than guessed.

---

## Table of Contents

1. [High-Level Overview](#1-high-level-overview)
2. [Repository Structure](#2-repository-structure)
3. [Technology Stack](#3-technology-stack)
4. [Component Breakdown](#4-component-breakdown)
5. [Execution Flow](#5-execution-flow)
6. [Data Flow](#6-data-flow)
7. [Important Design Patterns](#7-important-design-patterns)
8. [Configuration](#8-configuration)
9. [Dependencies](#9-dependencies)
10. [GitHub Project Analysis](#10-github-project-analysis)
11. [Testing](#11-testing)
12. [Important Files](#12-important-files)
13. [Code Quality](#13-code-quality)
14. [Learning Path](#14-learning-path)
15. [Developer Workflow](#15-developer-workflow)
16. [Improvement Opportunities](#16-improvement-opportunities)

---

## 1. High-Level Overview

### Problem Solved

Git is the command surface where a wrong invocation hurts most — `push --force`,
`reset --hard`, `branch -D`, a botched `rebase` can silently destroy work or
rewrite shared history. Git Copilot lets a user state a git goal in plain English
and have a local LLM propose commands one at a time, **but the decision to run
a destructive command is never trusted to the model**. A deterministic,
flag-aware classifier and a human-confirmation gate sit between the model and
the real `git` binary.

The central thesis (stated in `README.md` and reinforced in every module
docstring) is: *the interesting part isn't the model — it's the guardrail design
around it*. The enforcement lives in code the model cannot route around.

### Primary Features

| Feature | Where it lives |
|--------|----------------|
| Natural-language → git command agent loop | `gitcopilot/agent.py` (`Agent.run`) |
| 3-tier permission classifier (read-only / mutating / destructive) | `gitcopilot/classifier.py` (`classify`) |
| Flag-aware classification (same subcommand, different tiers) | `gitcopilot/classifier.py` (`_classify_branch`, `_classify_push`, etc.) |
| Plan-then-apply blast-radius previews (read-only) | `gitcopilot/preview.py` (`build_preview`) |
| Repo-state guard (detached HEAD, conflicts, ahead/behind, dirty) | `gitcopilot/repostate.py` (`snapshot`) |
| Structured tool-calling (no prose parsing) | `gitcopilot/planner/base.py` (`ToolCall`, `TOOL_SCHEMA`) |
| Offline deterministic planner (CI/demos, no model) | `gitcopilot/planner/offline.py` (`OfflinePlanner`) |
| Ollama-backed planner (local LLM, JSON-schema constrained) | `gitcopilot/planner/ollama.py` (`OllamaPlanner`) |
| JSON-lines transcript + `replay` | `gitcopilot/transcript.py` (`Transcript`) |
| Shell-free execution, timeouts, no-pager/no-prompt | `gitcopilot/gitcmd.py` (`GitRunner`) |
| Explain mode (propose + classify, run nothing) | `gitcopilot/agent.py` (`AgentConfig.explain_only`) + `gitcopilot/cli.py` (`ExplainPrinter`) |

### Intended Users

Developers who want AI assistance for git but are wary of a model-chosen command
eating their work. The `--explain` mode targets learners; `--yes` targets
throwaway/scratch repos and CI; the default interactive mode targets real
working repos where a human must confirm anything mutating or destructive.

### Overall Architecture

```
goal ──▶ Planner ──▶ ToolCall(run_git, argv) ──▶ Classifier ──▶ tier
                                                       │
                          read-only ─ auto-run ◀───────┤
                          mutating  ─ confirm  ◀────────┤
                          destructive ─ preview+confirm ◀┘
                                   │
                          run git ─┴─▶ output ──▶ (fed back to Planner) ──▶ …done
```

The architecture is a single **observe → decide → act loop** specialized for git.
The planner is a pluggable Strategy (`Planner` Protocol); the safety gate is
deterministic code in `agent.py` + `classifier.py` + `preview.py`. There is no
server — it is a CLI that shells out to the real `git` binary via a safe wrapper.

---

## 2. Repository Structure

```
git-copilot-main/
├── .github/
│   └── workflows/
│       └── ci.yml              # CI: matrix test (3.9/3.11/3.12) + CLI smoke test
├── .gitignore                  # ignores .venv, .gitcopilot/, scratch-repo/, build artifacts
├── assets/
│   ├── demo.gif                # recorded README demo
│   └── demo.tape               # VHS tape source to regenerate the gif
├── gitcopilot/                 # the Python package
│   ├── __init__.py             # exports classify, Classification, Tier, __version__
│   ├── __main__.py             # `python -m gitcopilot` entry → cli.main
│   ├── agent.py                # the observe→decide→act loop + permission gate
│   ├── classifier.py           # flag-aware 3-tier command classifier (safety core)
│   ├── cli.py                  # argparse CLI, ANSI UI, confirmers, transcript wiring
│   ├── gitcmd.py               # safe subprocess wrapper around the git binary
│   ├── preview.py              # read-only blast-radius previews for destructive cmds
│   ├── repostate.py            # repo snapshot guard (detached/conflict/dirty/ahead)
│   ├── transcript.py          # JSON-lines log + replay
│   └── planner/
│       ├── __init__.py         # re-exports Planner, ToolCall, build_planner
│       ├── base.py             # Planner Protocol, ToolCall, Step, TOOL_SCHEMA
│       ├── offline.py          # deterministic keyword→command planner
│       └── ollama.py           # Ollama HTTP planner (urllib, JSON-schema constrained)
├── scripts/
│   └── make_scratch_repo.sh    # builds a throwaway repo with history + dirty state
├── tests/
│   ├── conftest.py             # fixtures: real throwaway git repos in tmp_path
│   ├── test_agent.py           # end-to-end loop tests (7)
│   ├── test_classifier.py      # classifier tier tests (9 funcs, 3 parametrize = 78 cases)
│   ├── test_offline_planner.py # offline planner intent tests (8)
│   └── test_repostate.py       # repo-state guard tests (6)
├── Makefile                    # install/test/demo/scratch/run/ollama-setup/lint/clean
├── pyproject.toml              # PEP 621 project metadata, zero runtime deps
└── README.md                   # user-facing docs + architecture summary
```

### Entry Points

There are two equivalent entry points, both delegating to `gitcopilot.cli.main`:

1. **Console script** (declared in `pyproject.toml` line 30):
   `gitcopilot = "gitcopilot.cli:main"` — installed by `pip install -e .`, provides
   the `gitcopilot` command.

2. **Module execution** (`gitcopilot/__main__.py`):
   ```python
   from .cli import main
   if __name__ == "__main__":
       raise SystemExit(main())
   ```
   Enables `python -m gitcopilot`.

### Startup & Execution Flow

1. The shell invokes `gitcopilot [flags] "<goal>"` (or `python -m gitcopilot ...`).
2. `gitcopilot/cli.py:main` parses args with `argparse` (line 204-215).
3. If `goal == "replay"`, it loads the transcript and re-prints it (line 223-224).
4. Otherwise it constructs a `GitRunner(cwd=args.repo)` and verifies it is a repo
   (line 229-234).
5. It builds a planner via `build_planner(planner_name, model=...)` (line 238).
6. It wires an `AgentConfig`, a `Printer`/`ExplainPrinter`, and a `Confirmer`
   (`always_yes` if `--yes`, else `interactive_confirmer`) (line 240-242).
7. It constructs `Agent(planner, runner, confirm, config, on_event=printer)`
   and calls `agent.run(goal)` (line 249-251).
8. On completion it writes a `TranscriptRecord` and returns the exit code
   (line 259-260).

---

## 3. Technology Stack

### Languages

- **Python 3.9+** (the only language; `requires-python = ">=3.9"` in
  `pyproject.toml`). Uses `from __future__ import annotations` everywhere for
  forward-compat typing, and PEP 604 unions (`str | None`) which require 3.10+
  at runtime but are strings under `__future__` annotations on 3.9.
- **Bash** — `scripts/make_scratch_repo.sh` and the `Makefile` recipes.
- **YAML** — `.github/workflows/ci.yml`.
- **TOML** — `pyproject.toml` (PEP 621).

### Frameworks & Libraries

- **No runtime frameworks.** The Ollama client is hand-rolled on
  `urllib.request` (`gitcopilot/planner/ollama.py:81-102`); ANSI output is
  hand-rolled (`gitcopilot/cli.py:38-58`). This is an explicit design choice
  documented in `pyproject.toml` line 16: *"Zero runtime dependencies: the
  Ollama client is stdlib urllib, output is hand-rolled ANSI."*
- **pytest** — dev-only test framework (`pyproject.toml` line 19).

### Build System

- **setuptools** (`build-backend = "setuptools.build_meta"`,
  `pyproject.toml` line 2-3). Package discovery via
  `[tool.setuptools.packages.find]` with `include = ["gitcopilot*"]`.

### Dependency Management

- Declared in `pyproject.toml`: `dependencies = []` (zero runtime deps),
  `dev = ["pytest>=7"]` (optional).
- Installed editable via `pip install -e ".[dev]"` (Makefile `install` target).

### External Services / APIs

- **Ollama** — local LLM daemon, HTTP API at `http://localhost:11434/api/chat`
  (default; overridable via `OLLAMA_HOST`). Used only by the `OllamaPlanner`;
  the `OfflinePlanner` and the entire test suite require no network and no model.
- **git** — the real `git` binary on `PATH`, invoked via `subprocess.run` with
  `shell=False` (`gitcopilot/gitcmd.py:68`).

---

## 4. Component Breakdown

### `gitcopilot/classifier.py` — The Safety Core

- **Purpose**: Classify any git command into one of three permission tiers.
- **Responsibilities**: Flag-aware tier assignment; fail-safe escalation;
  network-op flagging; config-injection detection.
- **Public interface**:
  - `classify(command: list[str] | str) -> Classification` (line 97)
  - `Tier` enum (`READ_ONLY=0`, `MUTATING=1`, `DESTRUCTIVE=2`) (line 32)
  - `Classification` dataclass (`tier`, `reason`, `touches_network`, `auto_runnable`) (line 52)
- **Dependencies**: only stdlib (`enum`, `shlex`, `dataclasses`).
- **Important functions**:
  - `classify` — normalizes input, dispatches to per-subcommand rules or falls
    back to allowlists (`_READ_ONLY_SUBCOMMANDS`, `_MUTATING_SUBCOMMANDS`).
  - `_classify_branch` (line 189) — the canonical example of flag-awareness:
    bare `branch` = read-only, `branch <name>` = mutating, `branch -D` = destructive.
  - `_classify_push` (line 269) — distinguishes `--force` vs `--force-with-lease`
    vs `--mirror` vs `--delete`.
  - `_classify_clean` (line 258) — `-n`/`--dry-run` is read-only; `-f` (even
    bundled like `-xdf`) is destructive.
  - `_short_flag_has` (line 169) — detects a letter inside a bundled short-flag
    token (e.g. `'f'` in `'-xdf'`).
  - `_is_config_injection` (line 382) — flags `git -c ...` which can inject
    dangerous config; such commands never auto-run.

### `gitcopilot/agent.py` — The Loop & Permission Gate

- **Purpose**: The observe→decide→act loop; the deterministic permission gate.
- **Responsibilities**: Snapshot repo state; ask planner for one action;
  classify; gate (auto-run / confirm / preview+confirm); execute or record denial;
  feed output back; enforce step budget.
- **Public interface**:
  - `Agent(planner, runner, confirm, config, on_event).run(goal) -> LoopResult` (line 77)
  - `AgentConfig` (line 47): `max_steps=12`, `explain_only=False`,
    `auto_read_only=True`, `retries=2`.
  - `Confirmer` type alias (line 43): `Callable[[ToolCall, Classification, Preview | None, RepoState], Decision]`.
  - `Decision(approved, reason)` (line 33).
  - Ready-made confirmers: `always_yes` (line 148), `always_no` (line 158).
- **Dependencies**: `classifier`, `gitcmd`, `planner.base`, `preview`, `repostate`.
- **Important functions**:
  - `Agent.run` (line 77) — the main loop. Iterates up to `max_steps`; each
    iteration: `snapshot` → `_decide` → classify → `_gate` → run or deny.
  - `Agent._decide` (line 120) — retries the planner up to `retries+1` times on
    `PlannerError`.
  - `Agent._gate` (line 131) — the gate logic: explain-only denies all;
    read-only auto-approves; destructive builds a preview before calling
    `confirm`.

### `gitcopilot/gitcmd.py` — Safe Git Execution

- **Purpose**: Run the real `git` binary safely.
- **Responsibilities**: argv-list execution with `shell=False`; inject
  `core.pager=cat` + `color.ui=never` + `GIT_TERMINAL_PROMPT=0` to prevent
  hangs; wall-clock timeout; structured `GitResult`.
- **Public interface**:
  - `GitRunner(cwd, timeout).run(argv) -> GitResult` (line 51)
  - `GitRunner.is_repo() -> bool` (line 88)
  - `GitResult` (line 16): `argv`, `returncode`, `stdout`, `stderr`, `timed_out`,
    `ok` property, `summary(max_chars=4000)` method.
- **Dependencies**: stdlib (`os`, `subprocess`, `dataclasses`).
- **Key detail**: On `TimeoutExpired`, returns a `GitResult` with
  `returncode=124` and `timed_out=True` rather than raising (line 76-83). On
  `FileNotFoundError` (git not installed), returns `returncode=127` (line 84-85).

### `gitcopilot/repostate.py` — Repo-State Guard

- **Purpose**: Snapshot the repo before each step so the agent never acts on a
  surprise state.
- **Responsibilities**: Detect detached HEAD, ahead/behind upstream, dirty
  working tree (staged/unstaged/untracked counts via porcelain v2), unmerged
  paths, and in-progress operations (merge/rebase/cherry-pick/revert/bisect).
- **Public interface**:
  - `snapshot(runner: GitRunner) -> RepoState` (line 74)
  - `RepoState` dataclass (line 26) with `one_line()` (line 44) and `warnings`.
- **Dependencies**: `gitcmd.GitRunner`, `os`.
- **Important functions**:
  - `snapshot` — runs `symbolic-ref`, `rev-parse`, `rev-list --count`,
    `status --porcelain=v2`, and `rev-parse --git-dir`, then
    `_detect_in_progress`.
  - `_detect_in_progress` (line 137) — checks for `rebase-merge`,
    `rebase-apply`, `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`,
    `BISECT_LOG` inside the git dir.

### `gitcopilot/preview.py` — Plan-Then-Apply Previews

- **Purpose**: Show the blast radius of a destructive command *before* it runs,
  built entirely from read-only git queries.
- **Responsibilities**: Per-subcommand preview builders for `reset`, `clean`,
  `push`, `branch`, `rebase`, `checkout`/`restore`, `stash`.
- **Public interface**:
  - `build_preview(argv, runner) -> Preview` (line 36)
  - `Preview(title, lines).render() -> str` (line 31)
- **Dependencies**: `gitcmd.GitRunner`.
- **Important functions**:
  - `_preview_reset` (line 64) — lists commits that will be dropped
    (`rev-list target..HEAD`) and, for `--hard`, the uncommitted changes
    (`diff --stat HEAD`).
  - `_preview_clean` (line 90) — reuses `git clean -n` dry-run output.
  - `_preview_push` (line 108) — computes ahead/behind and warns about
    force-push overwriting remote commits; recommends `--force-with-lease`.
  - `_preview_branch` (line 127) — for `-D`, checks if the branch is unmerged
    and warns that its commits become unreachable.

### `gitcopilot/planner/base.py` — The Tool-Call Contract

- **Purpose**: Define the `Planner` Protocol and the structured `ToolCall` that
  every planner must return.
- **Responsibilities**: Constrain the model to exactly two actions
  (`run_git` or `done`); validate JSON replies; define the JSON schema
  advertised to structured-output backends.
- **Public interface**:
  - `Planner` Protocol (line 82): `name: str`, `next_action(goal, history) -> ToolCall`.
  - `ToolCall` dataclass (line 27): `action`, `argv`, `rationale`, `summary`;
    `ToolCall.from_json(obj)` (line 36) validates and raises `PlannerError` on
    malformed input.
  - `Step` dataclass (line 53): one recorded turn of the loop.
  - `TOOL_SCHEMA` (line 65): the JSON schema passed to Ollama's `format` field.
  - `PlannerError` (line 22).
- **Key design**: The agent never parses free-form prose. A malformed reply is
  a validation error the loop retries (`agent._decide`), not a mystery string.

### `gitcopilot/planner/offline.py` — Deterministic Backend

- **Purpose**: A model-free planner so demos and CI run with no Ollama, no
  network, deterministically.
- **Responsibilities**: Map common natural-language intents ("show history",
  "undo last commit", "delete branch", "force push", "stage all", "what
  changed") to fixed command sequences via keyword rules.
- **Public interface**: `OfflinePlanner.next_action(goal, history) -> ToolCall` (line 27).
- **Important functions**:
  - `_plan_for` (line 38) — the intent matcher; order matters (most specific
    first). Returns a list of `(argv, rationale)` tuples.
  - `_extract_branch` (line 104) — regex-extracts a branch name from the goal.
  - `_summarize` (line 113) — builds the `done` summary from executed history.
- **Design note** (from docstring): *"intentionally simple, not clever... the
  real intelligence is the Ollama planner."*

### `gitcopilot/planner/ollama.py` — LLM Backend

- **Purpose**: Use a local Ollama LLM as the planner, constrained to the
  `TOOL_SCHEMA` JSON schema.
- **Responsibilities**: Build the chat messages (system prompt + goal + history
  as alternating assistant/user turns); POST to `/api/chat` with
  `format=TOOL_SCHEMA` and `temperature=0.0`; parse JSON; validate via
  `ToolCall.from_json`.
- **Public interface**:
  - `OllamaPlanner(model, host, timeout).next_action(goal, history) -> ToolCall` (line 53)
  - `build_planner(name, **kw) -> Planner` (line 105) — factory used by the CLI.
- **Dependencies**: stdlib (`json`, `os`, `urllib`), `planner.base`.
- **Key details**:
  - System prompt (line 26-42) encodes operating rules but the docstring is
    explicit (line 12-14): *"The enforcement of those rules is not trusted to
    the prompt — the classifier + permission gate are the real guardrail."*
  - On `URLError`, raises `PlannerError` with a helpful message pointing to
    `--offline` (line 99-101).
  - Denied commands are fed back to the model as a user message so it can pivot
    (line 67-72).

### `gitcopilot/transcript.py` — Audit Log

- **Purpose**: Append-only JSON-lines log of every run for review and `replay`.
- **Public interface**:
  - `Transcript(path).write(record)` (line 41), `.load() -> list[TranscriptRecord]` (line 48)
  - `TranscriptRecord` (line 28), `TranscriptStep` (line 17).
- **Key detail**: `started_at` is passed in by the caller (CLI), not read from
  a clock in the core — keeping the core pure/testable (line 33 comment).

### `gitcopilot/cli.py` — Terminal UI & Wiring

- **Purpose**: Parse args, wire up the agent, render events, handle
  confirmation prompts and `replay`.
- **Responsibilities**: ANSI colors (`C` class), tier badges, interactive
  confirmer, event printers (`Printer`, `ExplainPrinter`), transcript record
  construction (`_record`), replay (`_replay`).
- **Public interface**: `main(argv=None) -> int` (line 204).
- **Important functions**:
  - `interactive_confirmer(state_getter) -> Confirmer` (line 75) — prints the
    tier badge, rationale, classification reason, network note, preview, and
    prompts `[y/N]`.
  - `Printer.__call__` (line 108) — renders `state`/`ran`/`denied`/`done`/
    `planner_error`/`planner_failed` events.
  - `_record` (line 160) — converts a `LoopResult` into a `TranscriptRecord`,
    re-classifying each step's argv (note: it re-parses exit codes from the
    output string — see [Code Quality](#13-code-quality)).

---

## 5. Execution Flow

Trace of a typical run: `gitcopilot --offline --repo scratch-repo "what changed in my working tree?"`

### Step-by-step

1. **Shell → `cli.main`** (`cli.py:204`). `argparse` parses `--offline`,
   `--repo scratch-repo`, and the goal string. `args.no_color` is False and
   stdout is a tty, so `C.enabled` stays True (line 217-218).

2. **Transcript init** (line 220): `Transcript(".gitcopilot/log.jsonl")`.

3. **Goal check** (line 222-227): goal is not `"replay"` and not empty, so
   proceeds.

4. **Runner + repo check** (line 229-234): `GitRunner(cwd="scratch-repo")`;
   `runner.is_repo()` runs `git rev-parse --is-inside-work-tree` → `"true"`.

5. **Planner build** (line 236-238): `planner_name = "offline"`;
   `build_planner("offline", model=None)` → `OfflinePlanner()` (via
   `ollama.py:105-110`).

6. **Config + printer + confirmer** (line 240-242):
   - `AgentConfig(max_steps=12, explain_only=False)`
   - `printer = Printer()`
   - `confirm = interactive_confirmer(runner)` (no `--yes`)

7. **Agent construction + run** (line 249-251):
   `Agent(planner, runner, confirm, config, on_event=printer).run(goal)`.

8. **Inside `Agent.run`** (`agent.py:77`), iteration 1 of up to 12:

   a. **OBSERVE** — `snapshot(self.runner)` (`repostate.py:74`): runs
      `symbolic-ref`, `rev-parse --short HEAD`, `rev-parse @{upstream}`,
      `status --porcelain=v2`, `rev-parse --git-dir`. Returns a `RepoState`
      with `dirty=True`, `unstaged=1`, `untracked=1`. Fires `on_event("state", ...)`.

   b. **DECIDE** — `self._decide(goal, [])` (`agent.py:120`): calls
      `planner.next_action("what changed...", [])`. `OfflinePlanner` matches the
      "diff/changed/changes" intent (`offline.py:88`) and returns
      `ToolCall("run_git", argv=["status","--short"], rationale="list changed files")`.

   c. **Classify** — `classify(["status","--short"])` (`classifier.py:97`):
      `status` is in `_READ_ONLY_SUBCOMMANDS` → `Classification(READ_ONLY, ...)`.

   d. **GATE** — `self._gate(call, cls, state)` (`agent.py:131`): not
      explain-only; `cls.tier == READ_ONLY and auto_read_only` → returns
      `Decision(True, "auto-approved (read-only)")`. No confirmer called.

   e. **ACT** — `self.runner.run(["status","--short"])` (`gitcmd.py:51`):
      builds `["git","-c","core.pager=cat","-c","color.ui=never","status","--short"]`,
      sets `GIT_PAGER=cat`, `GIT_TERMINAL_PROMPT=0`, runs `subprocess.run` with
      `timeout=30`. Returns `GitResult`. `step.output = res.summary()`. Fires
      `on_event("ran", ...)` → `Printer` prints `✓ git status --short (exit 0)`.

9. **Iteration 2**: history now has 1 step. `OfflinePlanner.next_action` returns
   the second item in the plan: `["diff","--stat"]`. Classified READ_ONLY,
   auto-run, output fed back.

10. **Iteration 3**: history has 2 steps; plan exhausted (len 2).
    `OfflinePlanner.next_action` returns `ToolCall("done", summary=...)` via
    `_summarize` (`offline.py:113`). `Agent.run` sees `call.action == "done"`,
    sets `result.summary`, fires `on_event("done", ...)`, breaks (line 88-91).

11. **Back in `cli.main`** (line 256-260): no `halted_reason`; writes
    `transcript.write(_record(...))`; returns 0.

### Destructive variant

For `gitcopilot --offline --explain --repo scratch-repo "undo my last commit but keep the changes"`:

- `OfflinePlanner` matches "undo + last + commit + keep" →
  `[["reset","--soft","HEAD~1"], ...]` (`offline.py:46-47`).
- `classify(["reset","--soft","HEAD~1"])` → `MUTATING` (`classifier.py:252-253`).
- `AgentConfig(explain_only=True)` → `_gate` returns
  `Decision(False, "explain-only mode: command not executed")` for every step.
- `ExplainPrinter` prints the tier badge + classification but runs nothing.

---

## 6. Data Flow

```
            ┌─────────────────────────────────────────────────────────────┐
            │  USER (stdin)                                                │
            │  goal: "undo my last commit but keep the changes"            │
            └───────────────────────────┬─────────────────────────────────┘
                                        │ str
                                        ▼
            ┌─────────────────────────────────────────────────────────────┐
            │  Planner.next_action(goal, history) -> ToolCall              │
            │  (OfflinePlanner: keyword rules; OllamaPlanner: HTTP+JSON)   │
            └───────────────────────────┬─────────────────────────────────┘
                                        │ ToolCall(action, argv, rationale)
                                        ▼
            ┌─────────────────────────────────────────────────────────────┐
            │  classifier.classify(argv) -> Classification(tier, reason)   │
            └───────────────────────────┬─────────────────────────────────┘
                                        │ tier
                                        ▼
            ┌─────────────────────────────────────────────────────────────┐
            │  Agent._gate                                                 │
            │  READ_ONLY  -> auto-approve                                   │
            │  MUTATING   -> Confirmer(call, cls, None, state)              │
            │  DESTRUCTIVE-> build_preview(argv, runner) -> Confirmer(...)  │
            └───────────────────────────┬─────────────────────────────────┘
                                        │ Decision(approved, reason)
                                        ▼
            ┌─────────────────────────────────────────────────────────────┐
            │  if approved: GitRunner.run(argv) -> GitResult                │
            │       step.output = GitResult.summary()  (truncated stdout)   │
            │  if denied:   step.executed=False, step.denied_reason=...     │
            └───────────────────────────┬─────────────────────────────────┘
                                        │ Step appended to history
                                        ▼
            ┌─────────────────────────────────────────────────────────────┐
            │  history fed back to Planner.next_action on next iteration     │
            │  (OllamaPlanner: denied cmds sent as user msg "was NOT run")  │
            └───────────────────────────┬─────────────────────────────────┘
                                        │ on "done" or step budget
                                        ▼
            ┌─────────────────────────────────────────────────────────────┐
            │  Transcript.write(TranscriptRecord)  -> .gitcopilot/log.jsonl │
            │  (stdout: Printer renders events; replay re-loads JSONL)      │
            └─────────────────────────────────────────────────────────────┘
```

### Where data enters

- The **goal string** enters via CLI args (`cli.py:222`).
- **Repo state** enters via `GitRunner.run` shelling out to git
  (`repostate.py:74`, `gitcmd.py:51`).
- **Model output** enters via Ollama HTTP response body
  (`ollama.py:96-102`).

### How it is transformed

- Goal → `ToolCall` by the planner (intent matching or LLM).
- `ToolCall.argv` → `Classification` by `classify`.
- `argv` + `GitRunner` → `Preview` by `build_preview` (for destructive).
- `argv` → `GitResult` by `GitRunner.run`; `GitResult.summary()` truncates to
  4000 chars (`gitcmd.py:28-38`).
- `LoopResult` → `TranscriptRecord` by `cli._record` (line 160).

### Where it is stored

- **`.gitcopilot/log.jsonl`** — append-only JSON-lines transcript
  (`transcript.py:41-46`).
- **`RepoState`** — ephemeral, recomputed each iteration.
- **`Step` list** — ephemeral in `LoopResult`, persisted only via transcript.

### How it leaves

- **stdout** — `Printer`/`ExplainPrinter` render events as ANSI text.
- **Transcript file** — written at end of run; readable via `gitcopilot replay`.
- **git repo** — side effects from approved mutating/destructive commands.

---

## 7. Important Design Patterns

### Strategy (Planners)

`Planner` is a `typing.Protocol` (`planner/base.py:82`) with two concrete
implementations: `OfflinePlanner` and `OllamaPlanner`. The `Agent` holds a
`Planner` and is unaware of which backend is active. The CLI selects via
`build_planner(name)` (`ollama.py:105`).

**Why**: Lets demos/CI run deterministically without a model while real users
get the LLM — same safety machinery either way.

### Dependency Injection (Confirmer & on_event)

`Agent.__init__` takes a `confirm: Confirmer` callable and an `on_event`
callback (`agent.py:63-75`). The CLI injects `interactive_confirmer`,
`always_yes`, or `always_no`; tests inject `ScriptedPlanner` + `always_yes`/
`always_no`/capturing closures.

**Why**: The core loop is pure and testable — no `input()` calls in `agent.py`.
The interactive UI is a swappable boundary.

### Factory (`build_planner`)

`build_planner(name, **kw) -> Planner` (`ollama.py:105-110`) constructs the
right planner from a string name.

**Why**: The CLI stays decoupled from concrete planner classes; adding a third
backend (e.g. an OpenAI planner) requires only a new module + one branch.

### Command Pattern (ToolCall)

`ToolCall` (`planner/base.py:27`) is a frozen dataclass representing exactly
one action (`run_git` or `done`) with its arguments. `ToolCall.from_json`
(line 36) is the parser/validator. The agent executes the command via
`GitRunner.run(call.argv)`.

**Why**: Structured tool-calling means the agent never parses model prose. A
malformed reply is a validation error, not a mystery string.

### Observer (on_event)

`Agent.on_event(kind, data)` (`agent.py:75`) is called at every significant
point (`state`, `propose`, `ran`, `denied`, `done`, `planner_error`,
`planner_failed`). The CLI's `Printer` is the concrete observer.

**Why**: Decouples the loop's progress reporting from its logic; the same
agent can drive a TUI, a test assertion, or a headless logger.

### Fail-Safe Defaults

The classifier escalates, never de-escalates: unknown subcommands default to
`MUTATING` (`classifier.py:159-160`); destructive flag patterns escalate even
benign subcommands. `Tier` is an `IntEnum` so `max(...)` picks the highest
(most dangerous) matching tier (`classifier.py:118`).

**Why**: Over-prompting is acceptable; silently running `reset --hard` is not.
This is the project's central safety invariant.

### Template Method (the loop)

`Agent.run` defines the skeleton (observe → decide → gate → act → repeat) with
fixed steps; the planner and confirmer are the variable parts injected by the
caller.

### Layered Defense (Defense in Depth)

1. **Planner prompt** asks the model to behave (`ollama.py:26-42`) — soft.
2. **Structured output** constrains the model to a JSON schema (`ollama.py:86`).
3. **Classifier** deterministically tiers the command — hard.
4. **Permission gate** enforces the tier — hard.
5. **Preview** shows blast radius before destructive runs — hard.
6. **Repo-state guard** warns on surprise states — soft (warn) + hard (block via confirmer).
7. **Step budget** halts runaway loops — hard.
8. **Shell-free execution + timeout** prevents injection/hangs — hard.

---

## 8. Configuration

### Configuration Files

- **`pyproject.toml`** — project metadata, entry point, package discovery,
  pytest config (`testpaths = ["tests"]`).
- **`Makefile`** — developer task runner (install/test/demo/scratch/run/clean).
- **`.github/workflows/ci.yml`** — CI pipeline definition.

### Environment Variables

| Variable | Where read | Default | Purpose |
|----------|-----------|---------|---------|
| `GITCOPILOT_MODEL` | `ollama.py:49` | `llama3.2` | Ollama model name |
| `OLLAMA_HOST` | `ollama.py:50` | `http://localhost:11434` | Ollama daemon URL |
| `GIT_PAGER` | `gitcmd.py:65` (set) | `cat` | Prevent pager hangs |
| `GIT_TERMINAL_PROMPT` | `gitcmd.py:66` (set) | `0` | Prevent credential-prompt hangs |
| `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE` | `scripts/make_scratch_repo.sh:17-18` | `2026-01-01T00:00:00` | Deterministic scratch repo |

### Runtime Options (CLI flags)

Defined in `cli.py:205-214`:

| Flag | Default | Purpose |
|------|---------|---------|
| `--offline` | off | use deterministic planner |
| `--model NAME` | env or `llama3.2` | Ollama model |
| `--repo PATH` | cwd | git working directory |
| `--explain` | off | propose + classify, run nothing |
| `--yes` | off | auto-approve gated commands (scratch only) |
| `--max-steps N` | 12 | step budget |
| `--transcript F` | `.gitcopilot/log.jsonl` | JSON-lines log path |
| `--no-color` | off | disable ANSI |

### Build Configuration

- `pyproject.toml` `[build-system]`: `setuptools>=61`, `setuptools.build_meta`.
- `[tool.setuptools.packages.find]`: `include = ["gitcopilot*"]`.
- `[tool.pytest.ini_options]`: `testpaths = ["tests"]`.

### Runtime Config Object

`AgentConfig` (`agent.py:47-51`): `max_steps`, `explain_only`, `auto_read_only`,
`retries`. Constructed in `cli.py:240`.

---

## 9. Dependencies

### Runtime Dependencies

**None.** `pyproject.toml` line 16: `dependencies = []`.

This is an explicit, documented design choice. The roles that a dependency
would normally fill are handled inline:

| Role | How it's handled without a dependency |
|------|---------------------------------------|
| Ollama client | `urllib.request` in `planner/ollama.py:81-102` |
| ANSI colors | hand-rolled `C` class in `cli.py:38-58` |
| JSON parsing | stdlib `json` |
| subprocess | stdlib `subprocess` |
| dataclasses | stdlib `dataclasses` |
| typing | stdlib `typing` (`Protocol`, `Literal`, `runtime_checkable`) |

**Why zero deps**: *"Everything runs offline for demos and CI."* (pyproject
comment). It also keeps the install trivial and the attack surface minimal.

### Dev Dependencies

- **`pytest>=7`** (`pyproject.toml` line 19, optional `[dev]`). Used for the
  entire test suite. No mocking library — tests use real git repos in tmp dirs
  (`conftest.py`).

### Implicit External Tools

- **`git`** — must be on `PATH`; invoked by `GitRunner` and by test fixtures.
- **`ollama`** — only needed for the real LLM planner; `make ollama-setup`
  pulls `llama3.2`.
- **`vhs`** — only to regenerate `assets/demo.gif` from `demo.tape`.

---

## 10. GitHub Project Analysis

### CI/CD Workflow

`.github/workflows/ci.yml` defines a single workflow `CI`:

- **Triggers**: `push` to `main`, and all `pull_request`s (line 3-6).
- **Runner**: `ubuntu-latest` (line 10).
- **Matrix**: Python `3.9`, `3.11`, `3.12` (line 12-13) — matches
  `requires-python = ">=3.9"`.
- **Steps**:
  1. `actions/checkout@v4`
  2. `actions/setup-python@v5` with the matrix version
  3. `pip install -e ".[dev]"` (editable install with pytest)
  4. Configure git globally (needed by integration tests that create commits)
  5. `pytest -q` — the full offline suite
  6. **Smoke test**: `./scripts/make_scratch_repo.sh` then two `gitcopilot`
     invocations (one read-only, one `--explain` destructive) against the
     scratch repo.

### Release Process

**Not present in the repository.** There is no release workflow, no
`CHANGELOG.md`, no tagged release automation, and no PyPI publish step. Version
is hardcoded as `0.1.0` in `pyproject.toml` line 13 and `__init__.py` line 10.
This cannot be determined from CI — it is inferred from the absence of any
release-related file or workflow.

### Testing Workflow

CI runs `pytest -q` with no Ollama and no network (the offline planner makes
this possible). The smoke test additionally exercises the installed `gitcopilot`
console script end-to-end. There is no separate staging/deploy job — this is a
library/CLI, not a deployed service.

---

## 11. Testing

### Framework

**pytest** (dev dependency, `pyproject.toml` line 19). Configured via
`[tool.pytest.ini_options]` with `testpaths = ["tests"]`.

### Test Organization

```
tests/
├── conftest.py             # shared fixtures (no tests)
├── test_agent.py           # 7 tests — end-to-end loop
├── test_classifier.py      # 9 functions, 3 @parametrize (72 cases) = 78 tests
├── test_offline_planner.py # 8 tests — intent→command mapping
└── test_repostate.py       # 6 tests — repo-state guard
```

**Total: 99 tests** (verified by counting: 7 + 78 + 8 + 6 = 99), matching the
README claim.

### How Tests Are Executed

- `make test` → `. .venv/bin/activate && pytest -q` (Makefile).
- CI: `pytest -q` directly after install (`.github/workflows/ci.yml`).

### Test Philosophy

Tests are **integration tests against real git**, not mocks
(`conftest.py:1-6` docstring): *"These are true integration fixtures — they
shell out to the actual git binary in an isolated temporary directory, so tests
exercise real porcelain output (status v2, rev-list, reflog) rather than mocks.
No network, no Ollama."*

Fixtures (`conftest.py`):
- `repo` — 3 commits, a dirty tracked file, an untracked file, a spare branch.
- `runner` — `GitRunner(cwd=str(repo))`.
- `clean_repo` — 1 commit, clean tree.

The agent tests use a `ScriptedPlanner` (`test_agent.py:16-27`) that emits a
fixed list of `ToolCall`s, so the loop's guardrails are tested deterministically
regardless of model behavior.

### Coverage by Module

| Module | Test file | Coverage depth |
|--------|-----------|-----------------|
| `classifier.py` | `test_classifier.py` | Deepest: every tier, flag-order edge cases, `-c` injection, network flag, empty input |
| `repostate.py` | `test_repostate.py` | Dirty/clean/detached/staged/merge-conflict/not-a-repo |
| `agent.py` | `test_agent.py` | Auto-run, confirmation, preview passthrough, explain mode, step budget, denial feedback, full offline flow |
| `planner/offline.py` | `test_offline_planner.py` | History/status/undo-soft/undo-hard/delete-branch/force-push/termination/stage-all |
| `planner/ollama.py` | (none) | **No tests** — cannot run without Ollama; untested in CI |
| `preview.py` | (indirect via `test_agent.py:51-65`) | Only asserts preview is non-None and mentions "commit"; builders not exhaustively tested |
| `gitcmd.py` | (indirect) | Exercised by every repo test; timeout/missing-git branches untested |
| `transcript.py` | (none) | **No direct tests** |
| `cli.py` | (smoke only) | CI smoke test runs two CLI invocations; no unit tests for `_record`/`_replay` |

### Current Test Coverage

Not measured (no coverage tool configured). Inferable: the safety-critical
modules (`classifier`, `repostate`, `agent`) are well covered; the I/O and UI
modules (`cli`, `transcript`, `ollama`) are lightly or not covered.

---

## 12. Important Files

Read in this order to build a mental model fastest:

1. **`README.md`** — the architecture summary and safety model table; the
   "why" before the "how".
2. **`gitcopilot/classifier.py`** — the safety core; the flag-aware tier rules
   are the heart of the project. Read `classify` + `_classify_branch` +
   `_classify_push` + `_classify_clean` first.
3. **`gitcopilot/planner/base.py`** — the `ToolCall` contract and `Planner`
   Protocol; understand the two-action structured-calling model before the
   planners.
4. **`gitcopilot/agent.py`** — the loop (`Agent.run`) and the gate
   (`Agent._gate`); how the classifier's tiers become runtime decisions.
5. **`gitcopilot/preview.py`** — how destructive commands get blast-radius
   previews from read-only queries.
6. **`gitcopilot/repostate.py`** — the surprise-state guard.
7. **`gitcopilot/gitcmd.py`** — the safe execution wrapper (short but critical).
8. **`gitcopilot/planner/offline.py`** — the deterministic backend; shows the
   intent→command pattern and makes the loop runnable without a model.
9. **`gitcopilot/planner/ollama.py`** — the real LLM backend; the system prompt
   and JSON-schema constraint.
10. **`gitcopilot/cli.py`** — the wiring (argparse, ANSI, confirmers, transcript).
11. **`gitcopilot/transcript.py`** — the audit log (small).
12. **`tests/conftest.py`** + **`tests/test_classifier.py`** — the fixtures and
    the deepest test coverage; reading tests clarifies intended behavior.

---

## 13. Code Quality

### Strengths

- **Fail-safe by construction.** Unknown subcommands → `MUTATING`; destructive
  flags escalate via `max(tier, ...)` (`classifier.py:118`). The `Tier` IntEnum
  ordering makes "escalate, never de-escalate" a one-liner.
- **No shell-injection surface.** Commands are always an argv list with
  `shell=False` (`gitcmd.py:68`), even though args come from an LLM.
- **Structured tool-calling, not prose parsing.** `ToolCall.from_json`
  validates; malformed replies are retried errors, not mysteries
  (`planner/base.py:36-50`, `agent.py:120-129`).
- **Zero runtime dependencies.** Keeps install trivial, attack surface
  minimal, and CI hermetic.
- **Pure core, swappable boundaries.** `Agent` has no `input()`/`print()`; the
  `Confirmer` and `on_event` callbacks isolate I/O. This makes the loop fully
  testable with `ScriptedPlanner` + `always_yes`/`always_no`.
- **Excellent docstrings.** Every module opens with a "why" paragraph; the
  classifier docstring explains the fail-safe philosophy explicitly
  (`classifier.py:18-23`).
- **Real integration tests.** Tests use real git in tmp dirs, not mocks, so
  porcelain-output parsing is actually validated.
- **Deterministic demos/CI.** The offline planner + deterministic scratch repo
  timestamps (`make_scratch_repo.sh:17-18`) make the demo reproducible.

### Potential Technical Debt

- **`cli._record` exit-code parsing hack** (`cli.py:167-175`): re-derives
  `exit_code` by string-scanning the `step.output` for `"(exit "`. This is
  fragile — it depends on `GitResult.summary()`'s exact format
  (`gitcmd.py:36`). A cleaner design would pass the `GitResult.returncode`
  directly through `Step` rather than round-tripping it through a string.
- **`OfflinePlanner` fragility** (`planner/offline.py`): keyword rules are
  order-sensitive and brittle. E.g. "delete branch the old one" would extract
  "the" as the branch name (the stoplist at `offline.py:107` catches "the" but
  not all articles). The planner is honest about being "intentionally simple,
  not clever" but any intent not in the hardcoded list falls through to
  `git status`.
- **No lint/format config.** The Makefile has a `lint` target in `.PHONY`
  (line 1) but no recipe — it does nothing. There is no `ruff`/`black`/
  `flake8` config in `pyproject.toml`. Style is enforced only by convention.
- **`repostate._detect_in_progress` path handling** (`repostate.py:140-141`):
  `os.path.join(runner.cwd, gitdir, *rel)` assumes `gitdir` is relative. If
  `git rev-parse --git-dir` returns an absolute path (which it can, e.g. with
  `--git-dir` or in some worktree setups), `os.path.join` will discard
  `runner.cwd` and use the absolute `gitdir` — which is actually correct
  behavior, but the comment/layout implies relative-path assumption. Not a bug
  per se, but a subtle correctness dependency on git's output.
- **`transcript.py` has no direct tests.** `Transcript.write`/`load` round-trip
  is untested; a schema change could silently break `replay`.
- **`OllamaPlanner` is untested.** No tests exercise the HTTP path, the
  system prompt, or the denied-command feedback message construction. This is
  understandable (needs a daemon) but means the real planner's message-building
  logic (`ollama.py:54-72`) could regress unnoticed.

### Areas That Appear Unfinished

- **`Makefile` `lint` target** is declared in `.PHONY` but has no recipe —
  effectively a no-op.
- **No `CHANGELOG.md`** or release process.
- **`always_no`** (`agent.py:158`) is defined but never wired by the CLI —
  only `always_yes` and `interactive_confirmer` are used. It exists for
  tests/dry-runs but there's no `--dry-run` flag to expose it.
- **`Classification.auto_runnable`** property (`classifier.py:64`) is defined
  but the agent checks `cls.tier == Tier.READ_ONLY` directly (`agent.py:136`)
  rather than using the property — minor redundancy.

### Code Smells

- **`cli._record` re-classifies** each step's argv (`cli.py:165`) by calling
  `classify` again, even though the agent already classified it at runtime
  (`agent.py:93`). The `Step.tier` is set but `_record` ignores it and
  recomputes — a sign the `Step` data model isn't carrying enough info.
- **`_TIER_STYLE` duplication** in `_replay` (`cli.py:193-194`): reconstructs a
  `Tier` lookup from string labels via a dict literal, duplicating
  `_TIER_STYLE`'s mapping. A `Tier.from_label` classmethod would centralize.
- **`Printer.__call__` is a long if/elif chain** (`cli.py:108-136`) — a
  dispatch dict or per-event methods would be cleaner, though for 6 branches
  it's tolerable.

### Potential Bugs

- **`repostate.py:127`** — `st.in_progress not in str(st.warnings)` is a
  substring check on the stringified warnings list to avoid duplicate
  warnings. This is fragile: if a warning happens to contain the word "merge"
  as a substring of another phrase, it could suppress a legitimate warning.
  Using a set of warning keys would be more robust.
- **`preview._preview_push`** (`preview.py:111-119`) — `counts.stdout.split()`
  is checked for `len(parts) == 2` but if the upstream is unset, `rev-list`
  fails and `counts.ok` is False, so it falls to the else branch correctly.
  However, `int(behind)` on line 117 could raise `ValueError` if git's output
  format changes — no try/except guards it.
- **`classifier._classify_checkout`** (`classifier.py:297`) — `"." in
  _positional(rest)` treats any positional argument equal to `"."` as
  destructive. But `git checkout .` (discard all) vs `git checkout main`
  (switch) are distinguished correctly; however `git checkout -- file` is
  caught by the `"--" in rest` check. The edge case: `git checkout --` with
  nothing after it would be classified destructive, which is safe (fail-safe)
  but possibly over-strict.
- **`offline._extract_branch`** (`offline.py:104-110`) — the regex
  `r"branch\s+['\"]?([\w./-]+)['\"]?"` would match "branch the" and extract
  "the"; the stoplist (`offline.py:107`) catches "the"/"a"/"this"/"my"/"named"/
  "called" but not "an" or "some". Minor, but could produce a confusing
  command like `git branch -d an`.

---

## 14. Learning Path

Recommended reading order for a new contributor:

1. **`README.md`** — the "why" and the safety-model table. Skim the architecture
   diagram.
2. **`gitcopilot/classifier.py`** — read `Tier`, `Classification`, then `classify`
   (the dispatcher), then 3-4 per-subcommand rules (`_classify_branch`,
   `_classify_push`, `_classify_clean`, `_classify_reset`). This is the safety
   core and the most important file.
3. **`gitcopilot/planner/base.py`** — `ToolCall`, `Planner` Protocol,
   `TOOL_SCHEMA`. Understand the two-action contract.
4. **`gitcopilot/agent.py`** — `Agent.run` (the loop) and `Agent._gate` (the
   gate). See how tiers become decisions.
5. **`gitcopilot/preview.py`** — `build_preview` + 2-3 builders
   (`_preview_reset`, `_preview_clean`, `_preview_push`).
6. **`gitcopilot/repostate.py`** — `snapshot` + `_detect_in_progress`.
7. **`gitcopilot/gitcmd.py`** — short; note the `shell=False`, timeout, and
  no-pager/no-prompt env injection.
8. **`gitcopilot/planner/offline.py`** — `_plan_for` (the intent matcher).
9. **`gitcopilot/planner/ollama.py`** — the system prompt + `_chat` + `next_action`.
10. **`gitcopilot/cli.py`** — `main` (wiring), `interactive_confirmer`,
    `Printer`, `_record`.
11. **`gitcopilot/transcript.py`** — small; the JSON-lines format.
12. **`tests/conftest.py`** + **`tests/test_classifier.py`** — the fixtures and
    the deepest coverage; tests clarify intended behavior better than docs.
13. **`scripts/make_scratch_repo.sh`** + **`Makefile`** + **`.github/workflows/ci.yml`**
    — the developer workflow.

---

## 15. Developer Workflow

### How to Install

```bash
make install
```
Which runs (Makefile):
```
python3 -m venv .venv && . .venv/bin/activate && \
    pip install -q --upgrade pip && pip install -e ".[dev]"
```
This creates a venv, installs the package editable, and adds `pytest`.

> **Note for Windows users**: The Makefile uses bash-style `. .venv/bin/activate`.
> On Windows (cmd.exe), the equivalent is `python -m venv .venv && .venv\Scripts\activate && pip install -e ".[dev]"`.

### How to Build

There is no separate build step — `pip install -e .` makes the package
importable and creates the `gitcopilot` console script. For a distributable
wheel/sdist, `python -m build` would work (setuptools backend is configured),
but no Makefile target exists for it.

### How to Run

```bash
# Offline demo on a throwaway repo (no model, no network):
make scratch                       # builds ./scratch-repo
gitcopilot --offline --repo scratch-repo "show me the recent history"

# Real LLM planner (needs `ollama serve` + `make ollama-setup`):
make run GOAL="squash my last three commits into one"
# or:
gitcopilot "squash my last three commits into one"
```

### How to Debug

- Add `--explain` to see every proposed command + classification without
  running anything (`cli.py:210`, `agent.py:133-134`).
- Inspect `.gitcopilot/log.jsonl` after a run, or `gitcopilot replay` to
  re-print past sessions (`cli.py:185-199`).
- The `Printer` emits `planner_error`/`planner_failed` events on malformed
  model replies (`cli.py:133-136`).
- For the Ollama path, set `OLLAMA_HOST` and check `ollama serve` is running;
  the error message in `ollama.py:99-101` points to `--offline` as a fallback.

### How to Execute Tests

```bash
make test
```
Which runs `. .venv/bin/activate && pytest -q`. All 99 tests are offline — no
Ollama, no network. Tests shell out to real git in tmp dirs.

### How to Lint/Format

**There is no configured linter or formatter.** The `Makefile` declares `lint`
in `.PHONY` (line 1) but provides no recipe — it is a no-op. There is no
`[tool.ruff]`, `[tool.black]`, or `[tool.flake8]` section in `pyproject.toml`.
Style is enforced only by convention and review. (See
[Improvement Opportunities](#16-improvement-opportunities).)

### Cleanup

```bash
make clean
```
Removes `.venv`, `scratch-repo`, `.gitcopilot`, egg-info, `__pycache__`.

---

## 16. Improvement Opportunities

### Architecture

- **Carry `GitResult` through `Step` instead of round-tripping through a
  string.** `cli._record` re-parses exit codes from `step.output`
  (`cli.py:167-175`); storing `returncode`/`timed_out` on `Step` directly would
  eliminate the fragile string scan and make the transcript more accurate.
- **Add a `--dry-run` flag** that wires `always_no` (`agent.py:158`). The
  confirmer exists but is unreachable from the CLI; a dry-run mode would be a
  natural complement to `--explain` (which doesn't even classify-then-skip,
  it skips at the gate).
- **Centralize tier-label mapping.** `Tier.label` exists
  (`classifier.py:43-49`) but `cli._replay` reconstructs a string→Tier dict
  (`cli.py:193-194`). A `Tier.from_label` classmethod would remove the
  duplication and the risk of drift.
- **Plugin hook for new subcommands.** The classifier's `dispatch` dict
  (`classifier.py:121-146`) is a closed registry; a registration decorator
  (e.g. `@classifier.register("submodule")`) would make it easier to extend
  and to test individual rules in isolation.

### Maintainability

- **Add a linter.** Wire `ruff` (or `flake8` + `black`) into `pyproject.toml`
  and give the `Makefile lint` target a real recipe. The codebase is small
  enough that adopting `ruff` would be near-zero-friction and would catch
  unused imports, undefined names, etc.
- **Add tests for `transcript.py` and `cli._record`/`_replay`.** These are
  untested and contain the fragile exit-code parsing hack; a round-trip test
  (`write` then `load` then assert equality) would lock in the schema.
- **Add tests for `OllamaPlanner.next_action` message construction.** Even
  without a daemon, the message-building logic (`ollama.py:54-72`) can be
  unit-tested by injecting a fake `_chat` — the class is structured to allow
  it.
- **Document the `Step` data model's relationship to `LoopResult` and
  `TranscriptStep`.** The three classes overlap but aren't 1:1; a diagram or a
  single mapping function would help contributors.

### Performance

- **`repostate.snapshot` runs ~6 git commands sequentially** per iteration
  (`repostate.py:74-134`). For a large repo these could be batched (e.g.
  `status --porcelain=v2 --branch` gives branch + ahead/behind + dirty in one
  call). Not a bottleneck for typical use, but reducible.
- **`GitResult.summary` truncates to 4000 chars** (`gitcmd.py:28`) — for very
  long outputs (e.g. `log` on a huge repo) this is fine, but the truncation
  point is hardcoded; making it configurable via `AgentConfig` would help
  power users.

### Readability

- **`Printer.__call__` if/elif chain** (`cli.py:108-136`) could become a
  dispatch dict mapping event-kind → handler method, improving extensibility
  and testability.
- **`classifier._positional`** (`classifier.py:178-186`) is documented as
  "crude" — it treats anything not starting with `-` as a positional, which
  misclassifies `git commit -m "msg with - dash"` style edge cases. A
  proper argv parser (even a small state machine) would be more correct, though
  the fail-safe default mitigates the risk.

### Documentation

- **Add a `CONTRIBUTING.md`** describing the classifier-extension pattern
  (add a subcommand to the right allowlist or a new `_classify_*` function +
  dispatch entry) and the test conventions (real git, not mocks).
- **Add a `CHANGELOG.md`** and a release process (even a manual tag + GitHub
  Release) — currently version is frozen at `0.1.0` with no history.
- **Document the Windows install path** in the README — the Makefile assumes
  bash; Windows users need the cmd equivalent (see
  [Developer Workflow](#15-developer-workflow)).
- **Add inline references from `cli._record` to `GitResult.summary`'s format**
  so the exit-code-parsing hack is at least visibly coupled to its dependency.

---

*End of report. Generated from a complete reading of every source, test, build,
CI, and script file in the repository.*