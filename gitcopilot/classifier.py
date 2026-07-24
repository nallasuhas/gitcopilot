"""Git command permission classifier.

This is the heart of Git Copilot's safety model. Every git command the agent
wants to run is classified into one of three tiers:

    READ_ONLY   -- observes the repo, changes nothing. Auto-run, no prompt.
    MUTATING    -- changes the repo but is recoverable (a commit, a branch
                   create, a staged file). Runs behind a yes/no confirmation.
    DESTRUCTIVE -- can lose work or rewrite shared history irreversibly
                   (force-push, hard reset, `clean -fd`, branch delete,
                   rebase). Gated with a preview of exactly what changes.

The classification is *flag-aware*: `git branch` lists (read-only), `git branch
new-feature` creates (mutating), `git branch -D old` deletes (destructive). The
same subcommand can land in any tier depending on its arguments, so the rules
below inspect the whole argument vector, not just the subcommand.

Design note: when a command is ambiguous or unrecognized we fail *safe* -- an
unknown subcommand is treated as MUTATING (confirm), and anything that pattern-
matches a destructive flag is escalated to DESTRUCTIVE even if the subcommand is
normally benign. It is always acceptable to over-prompt; it is never acceptable
to silently run something that eats the user's work.
"""

from __future__ import annotations

import enum
import shlex
from dataclasses import dataclass


class Tier(enum.IntEnum):
    """Permission tiers, ordered least -> most dangerous.

    Ordering matters: when several rules match a command we keep the *highest*
    tier (escalate, never de-escalate).
    """

    READ_ONLY = 0
    MUTATING = 1
    DESTRUCTIVE = 2

    @property
    def label(self) -> str:
        return {
            Tier.READ_ONLY: "read-only",
            Tier.MUTATING: "mutating",
            Tier.DESTRUCTIVE: "destructive",
        }[self]


@dataclass(frozen=True)
class Classification:
    """The result of classifying one git invocation."""

    tier: Tier
    reason: str
    #: Whether the command reaches the network (fetch/push/clone/ls-remote).
    #: Surfaced separately because a network op can be slow or leak, even when
    #: it is otherwise read-only or merely mutating.
    touches_network: bool = False

    @property
    def auto_runnable(self) -> bool:
        return self.tier == Tier.READ_ONLY


# --- Subcommand base tiers ---------------------------------------------------
# The tier a subcommand gets when no flag rule escalates it. Anything not listed
# here is unknown and treated as MUTATING (fail safe -> confirm).

_READ_ONLY_SUBCOMMANDS = {
    "status", "log", "show", "diff", "shortlog", "reflog", "blame", "annotate",
    "describe", "rev-parse", "rev-list", "name-rev", "merge-base", "cat-file",
    "ls-files", "ls-tree", "show-ref", "for-each-ref", "symbolic-ref",
    "count-objects", "grep", "whatchanged", "var", "help", "version",
    "check-ignore", "check-attr", "verify-commit", "verify-tag", "cherry",
    "range-diff", "diff-tree", "diff-index", "diff-files",
}

# Subcommands whose *default* form mutates but is recoverable.
_MUTATING_SUBCOMMANDS = {
    "add", "commit", "merge", "cherry-pick", "revert", "pull", "fetch",
    "mv", "restore", "switch", "checkout", "notes", "am", "apply",
    "format-patch", "bundle", "worktree", "sparse-checkout", "maintenance",
    "pack-refs", "repack", "prune-packed", "commit-tree", "hash-object",
    "write-tree", "read-tree", "update-index", "symbolic-ref-write",
}

# Subcommands that are context-dependent -- handled by dedicated rules below.
# (branch, tag, remote, config, stash, reset, clean, push, rebase, gc,
#  filter-branch, filter-repo, update-ref, replace, init, clone)

_NETWORK_SUBCOMMANDS = {"fetch", "pull", "push", "clone", "ls-remote", "remote"}


def classify(command: list[str] | str) -> Classification:
    """Classify a git command.

    Accepts either an argv list (``["git", "status"]`` or ``["status"]``) or a
    raw string (``"git status --short"``). A leading ``git`` token is optional
    and stripped.
    """
    argv = _normalize(command)
    if not argv:
        return Classification(Tier.READ_ONLY, "empty command", False)

    sub = argv[0]
    rest = argv[1:]
    network = sub in _NETWORK_SUBCOMMANDS

    # Global `-c key=value` overrides can inject dangerous config. Strip and
    # flag them, but if present, never auto-run.
    if sub in {"-c", "-C", "--exec-path", "--git-dir", "--work-tree"}:
        # A global option leaked into position 0; re-classify the tail but
        # never treat the whole thing as read-only.
        inner = classify(rest)
        tier = max(inner.tier, Tier.MUTATING) if _is_config_injection(argv) else inner.tier
        return Classification(tier, inner.reason, inner.touches_network)

    dispatch = {
        "branch": _classify_branch,
        "tag": _classify_tag,
        "remote": _classify_remote,
        "config": _classify_config,
        "stash": _classify_stash,
        "reset": _classify_reset,
        "clean": _classify_clean,
        "push": _classify_push,
        "rebase": _classify_rebase,
        "checkout": _classify_checkout,
        "restore": _classify_restore,
        "switch": _classify_switch,
        "gc": _classify_gc,
        "update-ref": _classify_update_ref,
        "filter-branch": lambda r: Classification(
            Tier.DESTRUCTIVE, "rewrites history across the whole repo", False),
        "filter-repo": lambda r: Classification(
            Tier.DESTRUCTIVE, "rewrites history across the whole repo", False),
        "replace": _classify_replace,
        "reflog": _classify_reflog,
        "init": lambda r: Classification(Tier.MUTATING, "initializes a repository", False),
        "clone": lambda r: Classification(Tier.MUTATING, "clones a repository", True),
        "bisect": _classify_bisect,
        "submodule": _classify_submodule,
    }

    if sub in dispatch:
        return dispatch[sub](rest)

    if sub in _READ_ONLY_SUBCOMMANDS:
        return Classification(Tier.READ_ONLY, f"`git {sub}` only reads repository state", network)

    if sub in _MUTATING_SUBCOMMANDS:
        return Classification(
            Tier.MUTATING, f"`git {sub}` changes the repo but is recoverable", network)

    # Unknown subcommand -> fail safe.
    return Classification(
        Tier.MUTATING, f"`git {sub}` is not in the allowlist; confirming to be safe", network)


# --- Per-subcommand rules ----------------------------------------------------

def _has(rest: list[str], *flags: str) -> bool:
    return any(a in flags for a in rest)


def _short_flag_has(rest: list[str], letter: str) -> bool:
    """True if any bundled short-flag token contains ``letter`` (e.g. 'f' in
    ``-xdf``). Ignores long options (``--force``) and non-flags."""
    for a in rest:
        if a.startswith("-") and not a.startswith("--") and letter in a[1:]:
            return True
    return False


def _positional(rest: list[str]) -> list[str]:
    """Return non-flag arguments (crude: anything not starting with '-')."""
    out = []
    for a in rest:
        if a == "--":
            continue
        if not a.startswith("-"):
            out.append(a)
    return out


def _classify_branch(rest: list[str]) -> Classification:
    if _has(rest, "-D") or _has(rest, "-d", "--delete"):
        return Classification(Tier.DESTRUCTIVE, "deletes a branch ref", False)
    if _has(rest, "-m", "--move", "-M", "-c", "--copy", "-C"):
        return Classification(Tier.MUTATING, "renames/copies a branch", False)
    if _has(rest, "--set-upstream-to", "-u", "--unset-upstream", "--edit-description"):
        return Classification(Tier.MUTATING, "changes branch configuration", False)
    # `git branch <name>` with a positional creates; bare `git branch` lists.
    if _positional(rest):
        return Classification(Tier.MUTATING, "creates a branch", False)
    return Classification(Tier.READ_ONLY, "lists branches", False)


def _classify_tag(rest: list[str]) -> Classification:
    if _has(rest, "-d", "--delete"):
        return Classification(Tier.DESTRUCTIVE, "deletes a tag ref", False)
    if _positional(rest) or _has(rest, "-a", "-s", "-m", "-f", "--force"):
        return Classification(Tier.MUTATING, "creates or moves a tag", False)
    return Classification(Tier.READ_ONLY, "lists tags", False)


def _classify_remote(rest: list[str]) -> Classification:
    if not rest:
        return Classification(Tier.READ_ONLY, "lists remotes", True)
    verb = rest[0]
    if verb in {"remove", "rm"}:
        return Classification(Tier.DESTRUCTIVE, "removes a remote and its tracking refs", False)
    if verb in {"add", "rename", "set-url", "set-head", "set-branches", "prune", "update"}:
        net = verb in {"prune", "update"}
        return Classification(Tier.MUTATING, f"`git remote {verb}` reconfigures remotes", net)
    # get-url / show / -v / (bare)
    return Classification(Tier.READ_ONLY, "inspects remote configuration", verb == "show")


def _classify_config(rest: list[str]) -> Classification:
    if _has(rest, "--unset", "--unset-all", "--remove-section", "--rename-section"):
        return Classification(Tier.DESTRUCTIVE, "removes configuration", False)
    read_flags = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch"}
    if _has(rest, *read_flags) or not _positional(rest):
        return Classification(Tier.READ_ONLY, "reads git configuration", False)
    # `git config user.name "X"` -> a set.
    return Classification(Tier.MUTATING, "writes git configuration", False)


def _classify_stash(rest: list[str]) -> Classification:
    verb = rest[0] if rest else "push"
    if verb in {"drop", "clear"}:
        return Classification(Tier.DESTRUCTIVE, "discards stashed changes permanently", False)
    if verb in {"list", "show"}:
        return Classification(Tier.READ_ONLY, "inspects the stash", False)
    if verb in {"pop", "apply", "push", "save", "create", "store", "branch"}:
        return Classification(Tier.MUTATING, f"`git stash {verb}` moves working-tree state", False)
    # bare `git stash` == push
    return Classification(Tier.MUTATING, "stashes working-tree changes", False)


def _classify_reset(rest: list[str]) -> Classification:
    if _has(rest, "--hard"):
        return Classification(
            Tier.DESTRUCTIVE, "`reset --hard` discards uncommitted changes in the working tree", False)
    if _has(rest, "--merge", "--keep"):
        return Classification(
            Tier.DESTRUCTIVE, "`reset --merge/--keep` can drop local changes", False)
    if _has(rest, "--soft"):
        return Classification(Tier.MUTATING, "moves HEAD, keeps index and working tree", False)
    # default (mixed) unstages; recoverable.
    return Classification(Tier.MUTATING, "moves HEAD and unstages changes (mixed)", False)


def _classify_clean(rest: list[str]) -> Classification:
    if _has(rest, "-n", "--dry-run") or _short_flag_has(rest, "n"):
        return Classification(Tier.READ_ONLY, "`clean --dry-run` only lists what would be removed", False)
    # -f may be bundled with other short flags in any order: -fd, -xdf, -fdx …
    if _has(rest, "--force") or _short_flag_has(rest, "f"):
        return Classification(
            Tier.DESTRUCTIVE, "`clean -f` permanently deletes untracked files", False)
    # clean without -f refuses to run; treat as read-only-ish but confirm.
    return Classification(Tier.MUTATING, "clean requires -f to act; confirming", False)


def _classify_push(rest: list[str]) -> Classification:
    force = _has(rest, "-f", "--force") or any(
        a == "--force-with-lease" or a.startswith("--force-with-lease=") for a in rest)
    if force:
        lease = any(a.startswith("--force-with-lease") for a in rest) and not _has(rest, "-f", "--force")
        why = ("`push --force-with-lease` rewrites the remote branch (safer, but still a rewrite)"
               if lease else
               "`push --force` overwrites remote history; other clones may lose commits")
        return Classification(Tier.DESTRUCTIVE, why, True)
    if _has(rest, "-d", "--delete") or any(a.startswith(":") for a in _positional(rest)):
        return Classification(Tier.DESTRUCTIVE, "deletes a branch on the remote", True)
    if _has(rest, "--mirror"):
        return Classification(Tier.DESTRUCTIVE, "`push --mirror` overwrites all remote refs", True)
    return Classification(Tier.MUTATING, "publishes local commits to a remote", True)


def _classify_rebase(rest: list[str]) -> Classification:
    if _has(rest, "--abort", "--quit"):
        return Classification(Tier.MUTATING, "aborts/exits an in-progress rebase", False)
    if _has(rest, "--continue", "--skip", "--edit-todo"):
        return Classification(Tier.MUTATING, "advances an in-progress rebase", False)
    # Starting/continuing a rebase rewrites commit history.
    return Classification(
        Tier.DESTRUCTIVE, "rebase rewrites commit history (new SHAs, moved commits)", False)


def _classify_checkout(rest: list[str]) -> Classification:
    # `git checkout -- <path>` or `git checkout .` discards working-tree changes.
    if "--" in rest or "." in _positional(rest):
        return Classification(
            Tier.DESTRUCTIVE, "`checkout <path>` discards uncommitted changes to those files", False)
    if _has(rest, "-f", "--force"):
        return Classification(Tier.DESTRUCTIVE, "`checkout --force` discards local changes", False)
    if _has(rest, "-b", "-B", "--orphan"):
        return Classification(Tier.MUTATING, "creates and switches to a new branch", False)
    # Switching branches with a dirty tree can fail but does not silently lose
    # work; treat as mutating.
    return Classification(Tier.MUTATING, "switches branches / checks out a ref", False)


def _classify_restore(rest: list[str]) -> Classification:
    staged_only = _has(rest, "--staged", "-S") and not _has(rest, "--worktree", "-W")
    if staged_only:
        return Classification(Tier.MUTATING, "unstages files (working tree untouched)", False)
    # Default restores the working tree from a source -> discards edits.
    return Classification(
        Tier.DESTRUCTIVE, "`restore` overwrites working-tree files, discarding edits", False)


def _classify_switch(rest: list[str]) -> Classification:
    if _has(rest, "-f", "--force", "--discard-changes"):
        return Classification(Tier.DESTRUCTIVE, "`switch --force` discards local changes", False)
    if _has(rest, "-c", "-C", "--create"):
        return Classification(Tier.MUTATING, "creates and switches to a branch", False)
    return Classification(Tier.MUTATING, "switches branches", False)


def _classify_gc(rest: list[str]) -> Classification:
    if _has(rest, "--prune=now") or any(a.startswith("--prune=") for a in rest):
        return Classification(
            Tier.DESTRUCTIVE, "`gc --prune` can drop unreachable objects (lost commits unrecoverable)", False)
    return Classification(Tier.MUTATING, "compacts the object database", False)


def _classify_update_ref(rest: list[str]) -> Classification:
    if _has(rest, "-d"):
        return Classification(Tier.DESTRUCTIVE, "deletes a ref directly", False)
    return Classification(Tier.DESTRUCTIVE, "rewrites a ref directly, bypassing safety checks", False)


def _classify_replace(rest: list[str]) -> Classification:
    if _has(rest, "-d", "--delete"):
        return Classification(Tier.MUTATING, "removes a replacement ref", False)
    if not _positional(rest) or _has(rest, "-l", "--list"):
        return Classification(Tier.READ_ONLY, "lists replacement refs", False)
    return Classification(Tier.DESTRUCTIVE, "creates a replace ref that rewrites object graph views", False)


def _classify_reflog(rest: list[str]) -> Classification:
    if rest and rest[0] in {"expire", "delete"}:
        return Classification(
            Tier.DESTRUCTIVE, "expiring the reflog removes your safety net for recovering lost commits", False)
    return Classification(Tier.READ_ONLY, "reads the reflog", False)


def _classify_bisect(rest: list[str]) -> Classification:
    verb = rest[0] if rest else ""
    if verb in {"log", "view", "visualize"}:
        return Classification(Tier.READ_ONLY, "inspects bisect state", False)
    return Classification(Tier.MUTATING, "drives a bisect session (checks out commits)", False)


def _classify_submodule(rest: list[str]) -> Classification:
    verb = rest[0] if rest else ""
    if verb in {"status", "summary", "foreach"} or not verb:
        return Classification(Tier.READ_ONLY, "inspects submodules", False)
    if verb == "deinit":
        return Classification(Tier.DESTRUCTIVE, "deinit removes a submodule's working tree", False)
    return Classification(Tier.MUTATING, f"`git submodule {verb}` changes submodule state", True)


# --- helpers -----------------------------------------------------------------

def _normalize(command: list[str] | str) -> list[str]:
    if isinstance(command, str):
        argv = shlex.split(command)
    else:
        argv = list(command)
    if argv and argv[0] == "git":
        argv = argv[1:]
    return argv


def _is_config_injection(argv: list[str]) -> bool:
    return "-c" in argv or "-C" in argv
