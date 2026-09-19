"""Repo-state guard.

Before (and after) every step the agent takes, we snapshot the repository so the
model -- and the user -- never act on a surprise state. The classic footguns
this catches:

  * **Detached HEAD** -- a commit made here is easy to lose.
  * **Unmerged paths** -- a merge/rebase is mid-flight; a naive `commit` or
    `checkout` would compound the mess.
  * **Ahead/behind** -- a `push` when you are behind, or a `reset` when you have
    unpushed commits, changes what "safe" means.
  * **Dirty working tree** -- a `checkout`/`reset` may discard uncommitted work.

The snapshot is built entirely from read-only porcelain commands, so producing
it is itself always safe to auto-run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .gitcmd import GitRunner


@dataclass
class RepoState:
    is_repo: bool = False
    branch: str | None = None          # None == detached HEAD
    detached: bool = False
    head_short: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty: bool = False
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    unmerged: int = 0                  # conflicted paths
    in_progress: str | None = None     # "merge" | "rebase" | "cherry-pick" | ...

    #: Human-readable warnings the UI should surface prominently.
    warnings: list[str] = field(default_factory=list)

    def one_line(self) -> str:
        if not self.is_repo:
            return "not a git repository"
        head = "HEAD (detached)" if self.detached else self.branch or "?"
        bits = [head]
        if self.head_short:
            bits.append(f"@{self.head_short}")
        track = []
        if self.ahead:
            track.append(f"↑{self.ahead}")
        if self.behind:
            track.append(f"↓{self.behind}")
        if track:
            bits.append("".join(track))
        state = []
        if self.staged:
            state.append(f"{self.staged} staged")
        if self.unstaged:
            state.append(f"{self.unstaged} unstaged")
        if self.untracked:
            state.append(f"{self.untracked} untracked")
        if self.unmerged:
            state.append(f"{self.unmerged} conflicted")
        clean = "clean" if not self.dirty and not self.unmerged else ", ".join(state)
        line = f"{' '.join(bits)} | {clean}"
        if self.in_progress:
            line += f" | {self.in_progress} in progress"
        return line


def snapshot(runner: GitRunner) -> RepoState:
    st = RepoState()
    if not runner.is_repo():
        st.warnings.append("Current directory is not inside a git work tree.")
        return st
    st.is_repo = True

    # Branch / detached HEAD.
    head = runner.run(["symbolic-ref", "--quiet", "--short", "HEAD"])
    if head.ok and head.stdout.strip():
        st.branch = head.stdout.strip()
    else:
        st.detached = True
        st.warnings.append(
            "Detached HEAD: commits made now aren't on any branch and are easy to lose.")
    st.head_short = runner.run(["rev-parse", "--short", "HEAD"]).stdout.strip() or None

    # Upstream + ahead/behind.
    up = runner.run(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if up.ok and up.stdout.strip():
        st.upstream = up.stdout.strip()
        counts = runner.run(["rev-list", "--left-right", "--count", "@{upstream}...HEAD"])
        if counts.ok:
            parts = counts.stdout.split()
            if len(parts) == 2:
                st.behind, st.ahead = int(parts[0]), int(parts[1])

    # Porcelain v2 gives staged/unstaged/untracked/conflicted in one shot.
    porc = runner.run(["status", "--porcelain=v2", "--untracked-files=all"])
    for line in porc.stdout.splitlines():
        if line.startswith("? "):
            st.untracked += 1
        elif line.startswith("u "):
            st.unmerged += 1
        elif line.startswith(("1 ", "2 ")):
            # Field 2 is the XY status; index (X) staged, worktree (Y) unstaged.
            xy = line.split(" ", 2)[1]
            if len(xy) == 2:
                if xy[0] != ".":
                    st.staged += 1
                if xy[1] != ".":
                    st.unstaged += 1
    st.dirty = bool(st.staged or st.unstaged or st.untracked)

    if st.unmerged:
        st.warnings.append(
            f"{st.unmerged} unmerged path(s): a merge/rebase is mid-conflict. "
            "Resolve or abort before other commands.")

    # In-progress operations, detected from the git dir layout.
    gitdir = runner.run(["rev-parse", "--git-dir"]).stdout.strip()
    if gitdir:
        st.in_progress = _detect_in_progress(runner, gitdir)
        if st.in_progress and st.in_progress not in ("", "merge") and st.in_progress not in str(st.warnings):
            st.warnings.append(f"A {st.in_progress} is in progress.")

    if st.behind and st.ahead:
        st.warnings.append(
            f"Diverged from {st.upstream}: {st.ahead} ahead, {st.behind} behind. "
            "A plain push will be rejected; a pull will merge/rebase.")
    return st


def _detect_in_progress(runner: GitRunner, gitdir: str) -> str | None:
    import os

    def exists(*rel: str) -> bool:
        return os.path.exists(os.path.join(runner.cwd, gitdir, *rel))

    if exists("rebase-merge") or exists("rebase-apply"):
        return "rebase"
    if exists("MERGE_HEAD"):
        return "merge"
    if exists("CHERRY_PICK_HEAD"):
        return "cherry-pick"
    if exists("REVERT_HEAD"):
        return "revert"
    if exists("BISECT_LOG"):
        return "bisect"
    return None
