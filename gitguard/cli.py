"""GitGuard command-line interface.

Usage:
    gitguard "undo my last commit but keep the changes"
    gitguard --offline "show me recent history"
    gitguard --explain "delete branch old-feature"   # explain, don't run
    gitguard --yes "stage everything"                 # auto-confirm (scratch!)
    gitguard replay                                   # re-print the transcript

Flags:
    --offline        use the deterministic planner (no Ollama, no network)
    --model NAME     Ollama model (default: env GITGUARD_MODEL or llama3.2)
    --repo PATH      run git in PATH (default: current directory)
    --explain        learning mode: propose + explain every command, run nothing
    --yes            auto-approve mutating/destructive commands (danger; scratch)
    --max-steps N    step budget before the loop halts (default 12)
    --transcript F   append a JSON-lines transcript to F (default .gitguard/log.jsonl)
    --no-color       disable ANSI color
"""

from __future__ import annotations

import argparse
import datetime
import sys

from .agent import Agent, AgentConfig, Confirmer, Decision, always_yes
from .classifier import Classification, Tier
from .gitcmd import GitRunner
from .planner.base import ToolCall
from .preview import Preview
from .repostate import RepoState
from .transcript import Transcript, TranscriptRecord, TranscriptStep

# --- tiny ANSI helper (no third-party deps) ----------------------------------


class C:
    enabled = True

    @classmethod
    def _w(cls, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if cls.enabled else s

    @classmethod
    def dim(cls, s): return cls._w("2", s)
    @classmethod
    def bold(cls, s): return cls._w("1", s)
    @classmethod
    def green(cls, s): return cls._w("32", s)
    @classmethod
    def yellow(cls, s): return cls._w("33", s)
    @classmethod
    def red(cls, s): return cls._w("1;31", s)
    @classmethod
    def cyan(cls, s): return cls._w("36", s)
    @classmethod
    def blue(cls, s): return cls._w("34", s)


_TIER_STYLE = {
    Tier.READ_ONLY: (C.green, "read-only"),
    Tier.MUTATING: (C.yellow, "mutating"),
    Tier.DESTRUCTIVE: (C.red, "DESTRUCTIVE"),
}


def _tier_badge(cls: Classification) -> str:
    style, label = _TIER_STYLE[cls.tier]
    return style(f"[{label}]")


# --- interactive confirmer ---------------------------------------------------

def interactive_confirmer(state_getter) -> Confirmer:
    def confirm(call: ToolCall, cls: Classification, preview: Preview | None, state: RepoState) -> Decision:
        print()
        print(f"  {_tier_badge(cls)} {C.bold('git ' + ' '.join(call.argv or []))}")
        if call.rationale:
            print(f"  {C.dim('why: ' + call.rationale)}")
        print(f"  {C.dim('note: ' + cls.reason)}")
        if cls.touches_network:
            print(f"  {C.dim('- reaches the network')}")
        if preview:
            print()
            for line in preview.render().splitlines():
                print(f"  {C.blue('|')} {line}")
        prompt = C.red("  Run this destructive command? [y/N] ") if cls.tier == Tier.DESTRUCTIVE \
            else C.yellow("  Run this? [y/N] ")
        try:
            ans = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return Decision(False, "aborted at prompt")
        if ans in ("y", "yes"):
            return Decision(True, "confirmed by user")
        return Decision(False, "declined by user")

    return confirm


# --- event printer -----------------------------------------------------------

class Printer:
    def __init__(self):
        self._last_state_line = None

    def __call__(self, kind: str, data: dict) -> None:
        if kind == "state":
            line = data["state"].one_line()
            if line != self._last_state_line:
                print(f"\n{C.cyan('repo:')} {line}")
                for w in data["state"].warnings:
                    print(f"  {C.yellow('! ' + w)}")
                self._last_state_line = line
        elif kind == "ran":
            res = data["result"]
            ok = C.green("[ok]") if res.ok else C.red("[FAIL]")
            tail = " (timed out)" if res.timed_out else ""
            print(f"  {ok} {C.dim('git ' + ' '.join(res.argv))}  {C.dim(f'(exit {res.returncode}{tail})')}")
            body = res.stdout.rstrip()
            if res.stderr.strip():
                body = (body + "\n" if body else "") + res.stderr.rstrip()
            lines = body.splitlines()
            for ln in lines[:40]:
                print(f"    {C.dim(ln)}")
            if len(lines) > 40:
                print(f"    {C.dim(f'... ({len(lines) - 40} more lines)')}")
        elif kind == "denied":
            print(f"  {C.dim('skipped: ' + data['reason'])}")
        elif kind == "done":
            print(f"\n{C.green('[done]')}  {data['summary']}")
        elif kind == "planner_error":
            print(f"  {C.yellow('planner returned an invalid action; retrying...')}")
        elif kind == "planner_failed":
            print(f"  {C.red('planner failed: ' + data.get('error', ''))}")


# --- explain-mode printer (propose only) -------------------------------------

class ExplainPrinter(Printer):
    def __call__(self, kind: str, data: dict) -> None:
        if kind == "propose":
            cls = data["cls"]
            call = data["call"]
            print(f"\n  {_tier_badge(cls)} {C.bold('git ' + ' '.join(call.argv or []))}")
            if call.rationale:
                print(f"  {C.dim('why: ' + call.rationale)}")
            print(f"  {C.dim('classification: ' + cls.reason)}")
        else:
            super().__call__(kind, data)


# --- transcript wiring -------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _record(goal: str, planner: str, cwd: str, result) -> TranscriptRecord:
    from .classifier import classify
    rec = TranscriptRecord(goal=goal, planner=planner, cwd=cwd, started_at=_now_iso(),
                           final_summary=result.summary or result.halted_reason)
    for s in result.steps:
        cls = classify(s.call.argv or [])
        exit_code = None
        if s.executed and s.output:
            first = s.output.splitlines()
            for ln in first:
                if "(exit " in ln:
                    try:
                        exit_code = int(ln.split("(exit ", 1)[1].split(")")[0].split(",")[0])
                    except (ValueError, IndexError):
                        pass
                    break
        rec.steps.append(TranscriptStep(
            argv=s.call.argv or [], tier=cls.tier.label, rationale=s.call.rationale,
            executed=s.executed, exit_code=exit_code,
            output_preview=(s.output[:500] if s.output else ""),
            denied_reason=s.denied_reason,
        ))
    return rec


def _replay(transcript: Transcript) -> int:
    records = transcript.load()
    if not records:
        print("No transcript found.")
        return 0
    for rec in records:
        print(f"\n{C.bold('> ' + rec.goal)}  {C.dim(f'[{rec.planner}] {rec.started_at}')}")
        for s in rec.steps:
            style, _ = _TIER_STYLE[{"read-only": Tier.READ_ONLY, "mutating": Tier.MUTATING,
                                    "destructive": Tier.DESTRUCTIVE}[s.tier]]
            mark = C.green("[ok]") if s.executed else C.dim("-")
            print(f"  {mark} {style('[' + s.tier + ']')} git {' '.join(s.argv)}"
                  + ("" if s.executed else f"  {C.dim('(' + s.denied_reason + ')')}"))
        print(f"  {C.cyan('-> ' + rec.final_summary)}")
    return 0


# --- entrypoint --------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gitguard", description="A safe, agentic git assistant.")
    p.add_argument("goal", nargs="*", help="natural-language git goal, or 'replay'")
    p.add_argument("--offline", action="store_true", help="use the model-free deterministic planner")
    p.add_argument("--model", default=None, help="Ollama model name")
    p.add_argument("--repo", default=None, help="git working directory (default: cwd)")
    p.add_argument("--explain", action="store_true", help="explain each command, run nothing")
    p.add_argument("--yes", action="store_true", help="auto-approve gated commands (scratch repos only!)")
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--transcript", default=".gitguard/log.jsonl")
    p.add_argument("--no-color", action="store_true")
    args = p.parse_args(argv)

    if args.no_color or not sys.stdout.isatty():
        C.enabled = False

    transcript = Transcript(args.transcript)

    goal = " ".join(args.goal).strip()
    if goal == "replay":
        return _replay(transcript)
    if not goal:
        p.print_help()
        return 2

    runner = GitRunner(cwd=args.repo)
    if not runner.is_repo():
        print(C.red(f"Not a git repository: {runner.cwd}"))
        print(C.dim("cd into a repo, pass --repo PATH, or make a scratch one: "
                    "scripts/make_scratch_repo.sh"))
        return 1

    planner_name = "offline" if args.offline else "ollama"
    from .planner.ollama import build_planner
    planner = build_planner(planner_name, model=args.model)

    config = AgentConfig(max_steps=args.max_steps, explain_only=args.explain)
    printer = ExplainPrinter() if args.explain else Printer()
    confirm: Confirmer = always_yes if args.yes else interactive_confirmer(runner)

    print(C.bold("GitGuard") + C.dim(f"  |  planner={planner_name}  |  repo={runner.cwd}"))
    print(C.dim(f"goal: {goal}"))
    if args.yes:
        print(C.red("! --yes: gated commands will run WITHOUT confirmation."))

    agent = Agent(planner, runner, confirm, config, on_event=printer)
    try:
        result = agent.run(goal)
    except KeyboardInterrupt:
        print(C.dim("\ninterrupted."))
        return 130

    if result.halted_reason:
        print(f"\n{C.yellow('[halted]')} {result.halted_reason}")

    transcript.write(_record(goal, planner_name, runner.cwd, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
