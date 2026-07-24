"""Shared fixtures: real, throwaway git repos in a tmp dir.

These are true integration fixtures -- they shell out to the actual git binary
in an isolated temporary directory, so tests exercise real porcelain output
(status v2, rev-list, reflog) rather than mocks. No network, no Ollama.
"""

import subprocess

import pytest

from gitcopilot.gitcmd import GitRunner


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A repo with 3 commits, a dirty tracked file, and an untracked file."""
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "Tester")

    (d / "file.txt").write_text("one\n")
    _git(d, "add", "file.txt")
    _git(d, "commit", "-q", "-m", "first")

    (d / "file.txt").write_text("one\ntwo\n")
    _git(d, "commit", "-qam", "second")

    (d / "README.md").write_text("# scratch\n")
    _git(d, "add", "README.md")
    _git(d, "commit", "-q", "-m", "third")

    # Dirty state for state-guard/preview tests.
    (d / "file.txt").write_text("one\ntwo\nthree uncommitted\n")
    (d / "untracked.local").write_text("x\n")
    _git(d, "branch", "old-experiment")

    return d


@pytest.fixture
def runner(repo):
    return GitRunner(cwd=str(repo))


@pytest.fixture
def clean_repo(tmp_path):
    """A repo with one commit and a clean working tree."""
    d = tmp_path / "clean"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "Tester")
    (d / "a.txt").write_text("a\n")
    _git(d, "add", "a.txt")
    _git(d, "commit", "-q", "-m", "only")
    return d
