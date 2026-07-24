"""The classifier is the safety centerpiece, so it gets the most tests.

Each case asserts the tier a git invocation lands in. The theme: the *same*
subcommand moves between tiers based on its flags/positionals, and anything
unrecognized or dangerous fails safe (escalates), never de-escalates.
"""

import pytest

from gitcopilot.classifier import Tier, classify


@pytest.mark.parametrize("cmd", [
    "git status",
    "status --short --branch",
    ["log", "--oneline", "-n", "10"],
    "git diff --stat",
    "show HEAD",
    "git branch",               # bare list
    "git branch -vv",
    "git tag",                  # bare list
    "git remote -v",
    "git remote get-url origin",
    "git config --get user.name",
    "git config --list",
    "git stash list",
    "git reflog",
    "git clean -n",             # dry-run
    "git clean --dry-run -d",
    "git rev-parse HEAD",
    "git merge-base main HEAD",
    "git bisect log",
    "git submodule status",
])
def test_read_only(cmd):
    assert classify(cmd).tier == Tier.READ_ONLY


@pytest.mark.parametrize("cmd", [
    "git add -A",
    "git commit -m hello",
    "git branch new-feature",         # create
    "git branch -m old new",          # rename
    "git tag v1.0",                   # create
    "git checkout main",              # switch branch
    "git checkout -b feature",        # create+switch
    "git switch main",
    "git merge feature",
    "git cherry-pick abc123",
    "git revert abc123",
    "git pull",
    "git fetch",
    "git stash",                      # push
    "git stash pop",
    "git reset HEAD~1",               # mixed (default)
    "git reset --soft HEAD~1",
    "git config user.name Shahab",    # set
    "git restore --staged file.txt",  # unstage only
    "git remote add origin url",
    "git init",
    "git mv a b",
    "git rebase --continue",
    "git totally-made-up-subcommand", # unknown -> fail safe to mutating
])
def test_mutating(cmd):
    assert classify(cmd).tier == Tier.MUTATING


@pytest.mark.parametrize("cmd", [
    "git push --force",
    "git push -f origin main",
    "git push --force-with-lease",
    "git push --mirror",
    "git push origin --delete feature",
    "git reset --hard",
    "git reset --hard HEAD~3",
    "git reset --keep HEAD~1",
    "git clean -fd",
    "git clean -f",
    "git clean -xdf",
    "git branch -D feature",
    "git branch -d merged",
    "git tag -d v1.0",
    "git checkout -- file.txt",       # discard working-tree edits
    "git checkout .",
    "git restore file.txt",           # default restores worktree
    "git switch --discard-changes main",
    "git rebase main",                # history rewrite
    "git rebase -i HEAD~3",
    "git gc --prune=now",
    "git filter-branch --tree-filter x HEAD",
    "git update-ref -d refs/heads/x",
    "git config --unset user.name",
    "git stash drop",
    "git stash clear",
    "git remote remove origin",
    "git reflog expire --expire=now --all",
])
def test_destructive(cmd):
    assert classify(cmd).tier == Tier.DESTRUCTIVE


def test_force_with_lease_reason_differs_from_plain_force():
    lease = classify("git push --force-with-lease")
    plain = classify("git push --force")
    assert lease.tier == plain.tier == Tier.DESTRUCTIVE
    assert "lease" in lease.reason.lower()
    assert "lease" not in plain.reason.lower() or "still" in plain.reason.lower()


def test_leading_git_optional_and_string_or_list():
    assert classify("status").tier == Tier.READ_ONLY
    assert classify(["git", "status"]).tier == Tier.READ_ONLY
    assert classify(["status"]).tier == Tier.READ_ONLY


def test_network_flag_surfaced():
    assert classify("git push").touches_network is True
    assert classify("git fetch").touches_network is True
    assert classify("git status").touches_network is False


def test_read_only_is_auto_runnable_others_not():
    assert classify("git log").auto_runnable is True
    assert classify("git commit -m x").auto_runnable is False
    assert classify("git reset --hard").auto_runnable is False


def test_empty_is_harmless():
    assert classify("").tier == Tier.READ_ONLY
    assert classify([]).tier == Tier.READ_ONLY


def test_config_injection_never_auto_runs():
    # `git -c ...` wrapping a read-only command must not silently auto-run.
    c = classify(["-c", "core.pager=evil", "status"])
    assert c.tier >= Tier.MUTATING
