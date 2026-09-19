"""Plan-then-apply: show what a mutating/destructive command will actually do
*before* it runs.

For the dangerous commands the classifier flags, a yes/no prompt isn't enough --
the user needs to see the blast radius. Each preview is built from read-only
git commands (dry-runs, rev-list counts, `diff --stat`), so generating the
preview never itself changes anything. Examples:

  reset --hard <ref>   -> which commits are dropped, which files revert
  clean -fd            -> the exact files/dirs that would be deleted (via -n)
  push --force         -> how many commits you'd overwrite on the remote
  branch -D <name>     -> the commit that would become unreachable
  rebase <base>        -> how many commits get replayed (new SHAs)

If we don't have a specific preview for a command we return a generic note so
the confirmation still carries the classifier's reason.
"""

from __future__ import annotations

from dataclasses import dataclass

from .gitcmd import GitRunner


@dataclass
class Preview:
    title: str
    lines: list[str]

    def render(self) -> str:
        body = "\n".join(f"  {ln}" for ln in self.lines) if self.lines else "  (nothing to preview)"
        return f"{self.title}\n{body}"


def build_preview(argv: list[str], runner: GitRunner) -> Preview:
    if not argv:
        return Preview("No command", [])
    sub = argv[0]
    rest = argv[1:]
    builders = {
        "reset": _preview_reset,
        "clean": _preview_clean,
        "push": _preview_push,
        "branch": _preview_branch,
        "rebase": _preview_rebase,
        "checkout": _preview_checkout_restore,
        "restore": _preview_checkout_restore,
        "stash": _preview_stash,
    }
    fn = builders.get(sub)
    if fn:
        return fn(rest, runner)
    return Preview(f"About to run: git {' '.join(argv)}", ["(no detailed preview available for this command)"])


def _target_ref(rest: list[str], default: str) -> str:
    for a in rest:
        if not a.startswith("-"):
            return a
    return default


def _preview_reset(rest: list[str], runner: GitRunner) -> Preview:
    hard = "--hard" in rest
    target = _target_ref(rest, "HEAD~1")
    lines = []
    dropped = runner.run(["rev-list", "--oneline", f"{target}..HEAD"])
    commits = [l for l in dropped.stdout.splitlines() if l.strip()]
    if commits:
        lines.append(f"{len(commits)} commit(s) will no longer be pointed to by this branch:")
        lines += [f"  - {c}" for c in commits[:10]]
        if len(commits) > 10:
            lines.append(f"  … and {len(commits) - 10} more")
        lines.append("(recoverable via `git reflog` until garbage-collected)")
    else:
        lines.append(f"HEAD is already at or behind {target}; no commits dropped.")
    if hard:
        diff = runner.run(["diff", "--stat", "HEAD"])
        changed = [l for l in diff.stdout.splitlines() if l.strip()]
        if changed:
            lines.append("")
            lines.append("--hard ALSO discards these uncommitted working-tree changes:")
            lines += [f"  {l}" for l in changed]
        else:
            lines.append("Working tree is clean; nothing uncommitted to lose.")
    return Preview(f"reset {target}" + (" --hard" if hard else ""), lines)


def _preview_clean(rest: list[str], runner: GitRunner) -> Preview:
    # Reuse git's own dry-run: exactly what -f would remove.
    dry = ["clean", "-n"]
    for f in rest:
        if f.startswith("-") and f not in ("-f", "--force", "-n", "--dry-run"):
            dry.append(f)
    res = runner.run(dry)
    items = [l.replace("Would remove ", "").strip() for l in res.stdout.splitlines() if l.strip()]
    if items:
        lines = [f"{len(items)} untracked path(s) will be permanently deleted:"]
        lines += [f"  - {i}" for i in items[:20]]
        if len(items) > 20:
            lines.append(f"  … and {len(items) - 20} more")
    else:
        lines = ["Nothing untracked to clean."]
    return Preview("clean (permanent deletion of untracked files)", lines)


def _preview_push(rest: list[str], runner: GitRunner) -> Preview:
    force = "--force" in rest or "-f" in rest or any(a.startswith("--force-with-lease") for a in rest)
    lines = []
    counts = runner.run(["rev-list", "--left-right", "--count", "@{upstream}...HEAD"])
    if counts.ok and counts.stdout.split():
        parts = counts.stdout.split()
        if len(parts) == 2:
            behind, ahead = parts
            lines.append(f"You are {ahead} ahead and {behind} behind the upstream.")
            if force and int(behind) > 0:
                lines.append(f"⚠ force-push will OVERWRITE {behind} commit(s) on the remote "
                             "that you don't have locally — other clones may lose them.")
    else:
        lines.append("No upstream tracking info; can't compute overwrite count.")
    if force:
        lines.append("Prefer --force-with-lease over --force to avoid clobbering others' pushes.")
    return Preview("push" + (" --force" if force else ""), lines)


def _preview_branch(rest: list[str], runner: GitRunner) -> Preview:
    if "-D" in rest or "-d" in rest or "--delete" in rest:
        name = _target_ref([a for a in rest if a not in ("-D", "-d", "--delete")], "")
        lines = []
        if name:
            tip = runner.run(["rev-list", "--oneline", "-n", "1", name])
            if tip.ok and tip.stdout.strip():
                lines.append(f"Branch '{name}' points at: {tip.stdout.strip()}")
            merged = runner.run(["branch", "--merged"])
            is_merged = any(name == l.strip().lstrip("* ").strip() for l in merged.stdout.splitlines())
            if "-D" in rest and not is_merged:
                lines.append("⚠ -D force-deletes even though this branch is NOT merged — "
                             "its unique commits become unreachable (reflog-recoverable for a while).")
            elif is_merged:
                lines.append("This branch is merged; its commits remain reachable elsewhere.")
        return Preview(f"delete branch {name}", lines or ["(branch not found)"])
    return Preview(f"git branch {' '.join(rest)}", ["Creates/updates a branch ref."])


def _preview_rebase(rest: list[str], runner: GitRunner) -> Preview:
    base = _target_ref(rest, "@{upstream}")
    replayed = runner.run(["rev-list", "--oneline", f"{base}..HEAD"])
    commits = [l for l in replayed.stdout.splitlines() if l.strip()]
    lines = [f"{len(commits)} commit(s) will be replayed onto {base} with NEW SHAs:"]
    lines += [f"  - {c}" for c in commits[:12]]
    if len(commits) > 12:
        lines.append(f"  … and {len(commits) - 12} more")
    lines.append("Anyone who based work on the old SHAs will need to re-sync.")
    return Preview(f"rebase onto {base}", lines if commits else ["Nothing to replay."])


def _preview_checkout_restore(rest: list[str], runner: GitRunner) -> Preview:
    diff = runner.run(["diff", "--stat"])
    changed = [l for l in diff.stdout.splitlines() if l.strip()]
    lines = (["These uncommitted changes would be overwritten:"] + [f"  {l}" for l in changed]
             if changed else ["Working tree is clean; nothing to discard."])
    return Preview("discard working-tree changes", lines)


def _preview_stash(rest: list[str], runner: GitRunner) -> Preview:
    if rest and rest[0] in ("drop", "clear"):
        res = runner.run(["stash", "list"])
        entries = [l for l in res.stdout.splitlines() if l.strip()]
        return Preview("drop stash", [f"{len(entries)} stash entr(ies) exist:"] + entries[:10])
    return Preview("stash", ["Moves working-tree changes onto the stash stack (recoverable)."])
