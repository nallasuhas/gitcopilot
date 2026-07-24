#!/usr/bin/env bash
# Build a throwaway git repo so you can watch Git Copilot drive git without
# touching any real work. Safe to delete and re-run any time.
#
#   ./scripts/make_scratch_repo.sh [dir]   # default: ./scratch-repo
set -euo pipefail

DIR="${1:-scratch-repo}"
rm -rf "$DIR"
mkdir -p "$DIR"
cd "$DIR"

git init -q
git config user.email "copilot@example.com"
git config user.name "Git Copilot Demo"
# Deterministic commit timestamps so demos/tests are reproducible.
export GIT_AUTHOR_DATE="2026-01-01T00:00:00"
export GIT_COMMITTER_DATE="2026-01-01T00:00:00"

printf 'line one\n' > file.txt
git add file.txt
git commit -q -m "first commit"

printf 'line one\nline two\n' > file.txt
git commit -qam "add line two"

printf '# scratch\n' > README.md
git add README.md
git commit -q -m "add README"

# Leave the working tree dirty + an untracked file so state-aware previews and
# the "what changed?" flow have something to show.
printf 'line one\nline two\nline three (uncommitted)\n' > file.txt
printf 'debug=true\n' > notes.local

# A spare branch to exercise branch-delete previews.
git branch old-experiment >/dev/null 2>&1 || true

echo "scratch repo ready at: $(pwd)"
echo "  try:  gitcopilot --offline --repo $DIR \"what changed in my working tree?\""
