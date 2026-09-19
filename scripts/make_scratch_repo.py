#!/usr/bin/env python3

"""

Build a throwaway git repo so you can watch GitGuard drive git without

touching any real work.



Usage:

    python scripts/make_scratch_repo.py [dir]



Default:

    scratch-repo

"""



from pathlib import Path

import os

import shutil

import subprocess

import sys



DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "scratch-repo")



if DIR.exists():

    shutil.rmtree(DIR)



DIR.mkdir(parents=True)



env = os.environ.copy()

env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00"

env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00"





def git(*args, check=True):

    subprocess.run(

        ["git", *args],

        cwd=DIR,

        env=env,

        check=check,

        stdout=subprocess.DEVNULL,

        stderr=subprocess.DEVNULL,

    )





# Initialize repository

git("init", "-q")



# Configure local identity

git("config", "user.email", "copilot@example.com")

git("config", "user.name", "GitGuard Demo")



# First commit

(DIR / "file.txt").write_text("line one\n", encoding="utf-8")

git("add", "file.txt")

git("commit", "-q", "-m", "first commit")



# Second commit

(DIR / "file.txt").write_text(

    "line one\nline two\n",

    encoding="utf-8",

)

git("commit", "-qam", "add line two")



# Third commit

(DIR / "README.md").write_text(

    "# scratch\n",

    encoding="utf-8",

)

git("add", "README.md")

git("commit", "-q", "-m", "add README")



# Leave the working tree dirty

(DIR / "file.txt").write_text(

    "line one\n"

    "line two\n"

    "line three (uncommitted)\n",

    encoding="utf-8",

)



# Leave an untracked file

(DIR / "notes.local").write_text(

    "debug=true\n",

    encoding="utf-8",

)



# Create spare branch (ignore if it somehow exists)

git("branch", "old-experiment", check=False)



print(f"scratch repo ready at: {DIR.resolve()}")

print(

    f'  try: gitguard --offline --repo {DIR} '

    '"what changed in my working tree?"'

)
