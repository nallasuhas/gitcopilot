"""Repo-state guard tests against real repos."""

from gitcopilot.gitcmd import GitRunner
from gitcopilot.repostate import snapshot


def test_dirty_repo_state(runner):
    st = snapshot(runner)
    assert st.is_repo
    assert st.branch in ("main", "master")
    assert st.detached is False
    assert st.dirty is True
    assert st.unstaged == 1        # file.txt modified
    assert st.untracked == 1       # untracked.local
    assert st.head_short


def test_clean_repo_state(clean_repo):
    st = snapshot(GitRunner(cwd=str(clean_repo)))
    assert st.is_repo
    assert st.dirty is False
    assert st.unstaged == 0
    assert st.untracked == 0
    assert "clean" in st.one_line()


def test_detached_head_warns(runner):
    head = runner.run(["rev-parse", "HEAD"]).stdout.strip()
    runner.run(["-c", "advice.detachedHead=false", "checkout", head])
    st = snapshot(runner)
    assert st.detached is True
    assert st.branch is None
    assert any("detached" in w.lower() for w in st.warnings)


def test_staged_counts(runner):
    runner.run(["add", "file.txt"])
    st = snapshot(runner)
    assert st.staged == 1


def test_merge_conflict_is_flagged(tmp_path):
    import subprocess

    def git(*a):
        subprocess.run(["git", *a], cwd=d, check=True, capture_output=True, text=True)

    d = tmp_path / "conflict"
    d.mkdir()
    git("init", "-q")
    git("config", "user.email", "t@e.com")
    git("config", "user.name", "T")
    (d / "f").write_text("base\n")
    git("add", "f"); git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "other")
    (d / "f").write_text("other change\n")
    git("commit", "-qam", "other")
    git("checkout", "-q", "-")
    (d / "f").write_text("main change\n")
    git("commit", "-qam", "main")
    # This merge conflicts; ignore the non-zero exit.
    subprocess.run(["git", "merge", "other"], cwd=d, capture_output=True, text=True)

    st = snapshot(GitRunner(cwd=str(d)))
    assert st.unmerged >= 1
    assert st.in_progress == "merge"
    assert any("unmerged" in w.lower() for w in st.warnings)


def test_not_a_repo(tmp_path):
    st = snapshot(GitRunner(cwd=str(tmp_path)))
    assert st.is_repo is False
    assert "not a git" in st.one_line().lower()
