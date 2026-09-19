# GitGuard — a safe, agentic git assistant

You type a goal in plain English. A local LLM proposes **one git command at a
time**, GitGuard classifies it, runs the harmless ones freely, and **stops to
show you the blast radius before anything destructive** — then reads the output
and continues. It's the same observe-decide-act loop an AI coding agent runs,
specialized for the command surface engineers fear most: **git**.

![GitGuard demo — a read-only goal auto-runs, then a destructive goal stops and previews exactly which commit and which uncommitted changes are at risk before asking to confirm](assets/demo.gif)

<sub>Read-only goals auto-run; a `reset --hard` stops and previews the blast radius before touching anything. (Recorded with the offline planner — no model required; regenerate via `vhs assets/demo.tape`.)</sub>

The interesting part isn't the model — it's the **guardrail design** around it.
Git is where a wrong command hurts (`push --force`, `reset --hard`, a branch
delete), so the whole project is built around *never letting a model-chosen
command eat your work without a human seeing exactly what it would do*.

---

## The safety model

Every command the agent proposes is sorted into one of three tiers by a
**flag-aware classifier** (`gitguard/classifier.py`). The tier decides the gate:

| Tier | Examples | Gate |
|------|----------|------|
| **read-only** | `status`, `log`, `diff`, `show`, `branch` (list), `clean -n` | **auto-run**, no prompt |
| **mutating** | `add`, `commit`, `checkout <branch>`, `pull`, `reset --soft` | **confirm** (y/N) |
| **destructive** | `push --force`, `reset --hard`, `clean -fd`, `branch -D`, `rebase` | **preview + confirm** |

The classification is **flag-aware**, because the same subcommand lives in
different tiers depending on its arguments:

```
git branch                 -> read-only  (lists branches)
git branch new-feature     -> mutating   (creates a branch)
git branch -D old-feature  -> destructive (deletes unmerged work)
```

Three principles keep it honest:

1. **Fail safe.** An unknown subcommand is treated as *mutating* (confirm), and
   anything matching a destructive pattern is escalated even if the subcommand
   is normally benign. Over-prompting is fine; silently running `reset --hard`
   is not.
2. **The gate is code, not a prompt.** The model is *asked* to behave, but the
   classifier + permission gate live in `agent.py` — so even a misbehaving or
   adversarial model can't run a destructive command without you seeing the
   preview first.
3. **Previews are read-only.** Every "here's what will happen" is built from
   dry-runs and `rev-list`/`diff --stat` counts, so generating the preview never
   changes anything.

### Plan-then-apply previews

For destructive commands a yes/no isn't enough — you need the blast radius
(`gitguard/preview.py`):

- `reset --hard <ref>` -> which commits get dropped **and** which uncommitted
  edits are lost
- `clean -fd` -> the exact untracked files that would be deleted (via `clean -n`)
- `push --force` -> how many upstream commits you'd overwrite
- `branch -D <name>` -> whether the branch is unmerged and what becomes unreachable
- `rebase <base>` -> how many commits get replayed with new SHAs

### Repo-state guard

Before every step GitGuard snapshots the repo (`gitguard/repostate.py`) so
you never act on a surprise state — **detached HEAD**, **unmerged paths**
mid-rebase, **ahead/behind** an upstream, or a **dirty** working tree are all
surfaced (and warned about) on each turn.

### Command validation

Before a proposed command reaches the classifier, it passes through a
**validator** (`gitguard/validator.py`) that checks it against the actual
repository state using read-only git queries:

- **Subcommand existence** — is `argv[0]` a real git subcommand? (via `git help -a`)
- **Ref existence** — do branch/tag refs referenced in the command actually exist?
  (via `git rev-parse --verify`)
- **Redundancy** — is the command a no-op given the current state? (e.g.
  switching to the branch you're already on)

A validation failure raises a `PlannerError` with a specific message, and the
agent's retry loop feeds it back to the planner so it can propose a corrected
command — the same pattern used for malformed JSON replies.

---

## Quickstart (Windows)

```cmd
:: One-time setup: create a venv and install the package + pytest
python -m venv .venv
call .venv\Scripts\activate.bat
pip install -e ".[dev]"

:: Run the test suite (fully offline -- no Ollama, no network)
pytest -q

:: Build a throwaway repo with some history + dirty state
python scripts\make_scratch_repo.py

:: End-to-end demo on the scratch repo, no model required:
gitguard --offline --repo scratch-repo "show me the recent history"
gitguard --offline --repo scratch-repo "what changed in my working tree?"
gitguard --offline --explain --repo scratch-repo "delete branch old-experiment"
```

To use the real LLM planner (a local [Ollama](https://ollama.com) daemon):

```cmd
ollama pull llama3.2
gitguard "squash my last three commits into one"
```

### CLI

```
gitguard [flags] "<natural-language goal>"
gitguard replay                  # re-print the transcript of past sessions

--offline        deterministic planner — no Ollama, no network (demos & CI)
--model NAME     Ollama model (default: $GITGUARD_MODEL or llama3.2)
--repo PATH      run git in PATH (default: current directory)
--explain        learning mode: propose + classify every command, run nothing
--yes            auto-approve gated commands (scratch repos only!)
--max-steps N    step budget before the loop halts (default 12)
--transcript F   JSON-lines log (default .gitguard/log.jsonl)
--no-color       disable ANSI color
```

---

## Architecture

```
goal --> Planner --> ToolCall(run_git, argv) --> Validator --> Classifier --> tier
                                                                  |
                               read-only  -- auto-run <-----------|
                               mutating   -- confirm  <-----------|
                               destructive -- preview+confirm <----|
                                          |
                               run git --> output --> (fed back to Planner) --> ...done
```

| Module | Responsibility |
|--------|----------------|
| `classifier.py` | flag-aware read-only / mutating / destructive tiers |
| `validator.py` | grounds planner proposals against real repo state (ref existence, subcommand validity) |
| `repostate.py` | the repo-state guard (branch, ahead/behind, dirty, conflicts) |
| `preview.py` | plan-then-apply blast-radius previews (read-only) |
| `agent.py` | the observe-decide-act loop + the permission gate + denied-command dedup |
| `gitcmd.py` | runs git with `shell=False`, timeouts, no pager/prompt hangs |
| `planner/` | `base` (the tool-call contract), `offline` (deterministic), `ollama` (LLM) |
| `transcript.py` | JSON-lines log + `replay` |
| `cli.py` | terminal UI, confirmation prompts, explain mode |

### Structured tool-calling, not text-parsing

The agent never parses model prose. Each planner returns exactly one validated
`ToolCall` — `run_git(argv, rationale)` or `done(summary)` — and the Ollama
backend uses Ollama's JSON-schema `format` to constrain output. A malformed
reply is a validation error the loop **retries**, not a mystery string.

### Offline / deterministic backend

Demos and CI must run with no model and no network, so there's a rule-based
`OfflinePlanner` that maps common intents ("show history", "undo my last
commit", "delete this branch", "switch to branch X") to fixed command sequences.
This is what lets the **entire safety machinery be tested deterministically**.

### Containment

- Commands are always an **argv list** run with `shell=False` — no shell means
  no shell-injection surface, even though the arguments come from an LLM.
- Every git call has a **wall-clock timeout**; `GIT_TERMINAL_PROMPT=0` and
  `core.pager=cat` stop it from ever blocking on a prompt or pager.
- A **step budget** halts a runaway loop (`--max-steps`, default 12).
- A **denied-command dedup** guard auto-skips repeated denied commands and halts
  if the planner is stuck re-proposing them.
- A **scratch-repo sandbox** (`scripts/make_scratch_repo.py`) gives demos and CI
  a throwaway target so nothing touches real work.

---

## Testing

```cmd
pytest -q
```

104 tests, all offline. The classifier gets the deepest coverage (every tier,
flag-order edge cases, `-c` config injection); the repo-state guard and agent
loop run against **real throwaway git repos** in a tmp dir (real porcelain
output, not mocks). CI (`.github/workflows/ci.yml`) runs the suite on Python
3.9-3.12 plus an end-to-end CLI smoke test — no Ollama required.

---

## Design notes

- **Why the gate lives in the classifier, not the prompt.** Trusting a model to
  self-police destructive commands is the wrong threat model; the enforcement is
  deterministic code the model can't route around.
- **The "surprise repo state" failure mode.** Detached HEAD and unmerged paths
  are where confident-but-wrong automation compounds a mess — so they're checked
  every turn and block/warn before the next command.
- **Plan-then-apply.** Previewing a rebase or a hard reset from read-only
  queries turns "trust me" into "here are the exact commits and files at risk."
- **Validation before classification.** The validator catches hallucinated
  branch names and invented subcommands *before* they reach the safety gate,
  feeding errors back to the planner for a retry — the same pattern used for
  malformed JSON.

## License

MIT
