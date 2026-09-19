"""Command validation layer — grounds planner proposals in real Git semantics.

Sits between the planner and the classifier in the agent loop. The planner
proposes a ``ToolCall``; the validator checks it against the *actual* repository
state using read-only git commands before it reaches the classifier/gate.

This is the architectural complement to the classifier: the classifier checks
*safety* (is it destructive?), the validator checks *correctness* (does this
command make sense given the current repo state?).

Validation checks (all read-only, all safe to auto-run):

1. **Subcommand existence** — is ``argv[0]`` a real git subcommand?
   (``git help -a`` parse, cached for the process lifetime.)
2. **Ref existence** — do branch/tag refs referenced in argv actually exist?
   (``git rev-parse --verify``.)
3. **Redundancy** — is the proposed command a no-op given the current state?
   (e.g. ``git switch main`` when already on ``main``.)

If a check fails, the validator raises :class:`PlannerError` with a specific
message. The agent's existing retry logic (``_decide``) feeds this back to the
planner so it can propose a corrected command — the same pattern used for
malformed JSON replies.

Design constraints honored:
- Does NOT replace or bypass the classifier — sits *before* it.
- Does NOT weaken safety — all checks are read-only.
- Does NOT change ``ToolCall`` or ``Planner`` interfaces.
- Does NOT add runtime dependencies — uses ``GitRunner`` (stdlib subprocess).
"""

from __future__ import annotations

from .gitcmd import GitRunner
from .planner.base import PlannerError, ToolCall
from .repostate import RepoState

# Subcommands that take a ref name as their first positional argument.
_REF_TAKING_SUBCOMMANDS = {
    "switch", "checkout", "merge", "rebase", "cherry-pick", "revert",
    "reset", "branch", "tag", "log", "show", "diff", "blame", "describe",
    "rev-parse", "rev-list", "merge-base", "name-rev", "push", "pull",
}

# Subcommands where the first positional is a *file path*, not a ref.
# (We skip ref-existence checks for these to avoid false positives.)
_PATH_TAKING_SUBCOMMANDS = {
    "add", "rm", "mv", "restore", "stash", "clean", "ls-files", "grep",
    "cat-file", "hash-object", "update-index",
}

# Special tokens that aren't refs and shouldn't be checked.
_NON_REF_TOKENS = {"HEAD", "HEAD~1", "HEAD^", "FETCH_HEAD", "ORIG_HEAD", ".", "..", "--"}


def validate(call: ToolCall, runner: GitRunner, state: RepoState) -> ToolCall:
    """Validate a planner-proposed ``ToolCall`` against real repo state.

    Returns the call unchanged if it passes all checks.
    Raises :class:`PlannerError` with a specific message if a check fails,
    so the agent's retry loop can ask the planner for a different command.
    """
    if call.action != "run_git":
        return call  # `done` calls need no validation

    argv = call.argv or []
    if not argv:
        return call  # empty is harmless; classifier handles it

    sub = argv[0]
    rest = argv[1:]

    # 1. Subcommand existence.
    _check_subcommand_exists(sub, runner)

    # 2. Ref existence for ref-taking subcommands.
    if sub in _REF_TAKING_SUBCOMMANDS and sub not in _PATH_TAKING_SUBCOMMANDS:
        _check_refs_exist(sub, rest, runner)

    # 3. Redundancy: switching to the branch we're already on.
    if sub in ("switch", "checkout") and not _has_flags(rest, "-c", "-C", "-b", "-B", "--create", "--orphan", "-f", "--force", "--discard-changes"):
        _check_redundant_switch(rest, state)

    return call


# --- internal helpers -------------------------------------------------------

def _check_subcommand_exists(sub: str, runner: GitRunner) -> None:
    """Verify ``sub`` is a real git subcommand via ``git help -a``."""
    known = _get_known_subcommands(runner)
    if sub not in known:
        raise PlannerError(
            f"`git {sub}` is not a recognized git subcommand. "
            f"Use a valid subcommand (run `git help -a` to see them)."
        )


def _check_refs_exist(sub: str, rest: list[str], runner: GitRunner) -> None:
    """Check that positional ref arguments actually exist in the repo."""
    positionals = [a for a in rest if not a.startswith("-") and a != "--"]
    if not positionals:
        return

    for arg in positionals:
        # Skip special tokens that aren't refs.
        if arg in _NON_REF_TOKENS:
            continue
        if arg.startswith("HEAD~") or arg.startswith("HEAD^"):
            continue
        if arg.startswith("@{") or arg.startswith(":"):
            continue
        # Check if this ref exists.
        if not _ref_exists(arg, runner):
            raise PlannerError(
                f"The ref '{arg}' does not exist in this repository. "
                f"Check the branch name or run `git branch --list` to see available branches."
            )


def _check_redundant_switch(rest: list[str], state: RepoState) -> None:
    """Detect switching to the branch we're already on."""
    if not state.is_repo or state.detached:
        return
    positionals = [a for a in rest if not a.startswith("-") and a != "--"]
    if positionals and state.branch and positionals[0] == state.branch:
        raise PlannerError(
            f"Already on branch '{state.branch}'. No need to switch."
        )


def _has_flags(rest: list[str], *flags: str) -> bool:
    return any(a in flags for a in rest)


def _ref_exists(ref: str, runner: GitRunner) -> bool:
    """True if ``ref`` resolves to a valid git object."""
    res = runner.run(["rev-parse", "--verify", f"{ref}^{{commit}}"])
    if res.ok and res.stdout.strip():
        return True
    # Also check if it's a valid branch name (not yet a commit, e.g. new branch).
    res2 = runner.run(["rev-parse", "--verify", ref])
    return res2.ok and bool(res2.stdout.strip())


# --- subcommand cache -------------------------------------------------------

_known_subcommands: set[str] | None = None


def _get_known_subcommands(runner: GitRunner) -> set[str]:
    """Return the set of git subcommands (cached for the process lifetime)."""
    global _known_subcommands
    if _known_subcommands is not None:
        return _known_subcommands
    res = runner.run(["help", "-a"])
    known: set[str] = set()
    for line in res.stdout.splitlines():
        line = line.strip()
        # Lines in `git help -a` look like: "   status      Show working tree status"
        parts = line.split(None, 1)
        if parts and not parts[0].startswith("-") and not parts[0].startswith("git"):
            # Filter out non-subcommand lines (headers, etc.)
            if len(parts[0]) <= 30 and " " in line:
                known.add(parts[0])
    # Always include common ones in case help -a parsing misses them.
    known.update({
        "status", "log", "show", "diff", "branch", "tag", "remote", "config",
        "stash", "reset", "clean", "push", "pull", "fetch", "clone", "init",
        "add", "commit", "merge", "rebase", "cherry-pick", "revert", "checkout",
        "switch", "restore", "mv", "rm", "grep", "blame", "annotate", "shortlog",
        "describe", "rev-parse", "rev-list", "name-rev", "merge-base", "cat-file",
        "ls-files", "ls-tree", "ls-remote", "show-ref", "for-each-ref",
        "symbolic-ref", "count-objects", "whatchanged", "var", "help", "version",
        "check-ignore", "check-attr", "verify-commit", "verify-tag", "cherry",
        "range-diff", "diff-tree", "diff-index", "diff-files", "reflog", "gc",
        "update-ref", "replace", "bisect", "submodule", "notes", "am", "apply",
        "format-patch", "bundle", "worktree", "sparse-checkout", "maintenance",
        "pack-refs", "repack", "prune-packed", "commit-tree", "hash-object",
        "write-tree", "read-tree", "update-index", "filter-branch", "filter-repo",
        "symbolic-ref-write", "archive", "fsck", "prune", "stripspace",
        "mktag", "verify-pack", "unpack-objects", "index-pack", "multi-pack-index",
        "credential", "credential-store", "credential-cache", "credential-osxkeychain",
        "fast-export", "fast-import", "mailsplit", "patch-id", "interpret-trailers",
        "hooks", "p4", "send-email", "svn", "citool", "gitk", "git-gui",
        "instaweb", "request-pull", "web--browse", "view",
    })
    _known_subcommands = known
    return known
