# VHS tape for the GitGuard README demo.
#   regenerate:  vhs assets/demo.tape   (from the repo root)
#
# Uses the offline planner, so the output is deterministic and needs no Ollama.
# Two scenes: a read-only goal auto-runs; a destructive goal stops and previews
# the blast radius before touching anything.
#
# Windows-compatible: uses cmd.exe, Windows path separators, and the Python
# scratch-repo script (no bash required).

Output assets/demo.gif

Set Shell cmd
Set FontSize 15
Set Width 1120
Set Height 760
Set Padding 22
Set TypingSpeed 35ms
Set Theme "Catppuccin Mocha"

# --- silent setup: venv on PATH + a throwaway repo to drive ------------------
Hide
Type "call .venv\Scripts\activate.bat && python scripts\make_scratch_repo.py >nul 2>&1 && cls"
Enter
Sleep 2s
Show

# --- Scene 1: read-only goal -> auto-runs, no prompt -------------------------
Type "gitguard --offline --repo scratch-repo "what changed in my working tree'"
Sleep 500ms 
Enter
Sleep 4s

Type "cls"
Enter
Sleep 500ms

# --- Scene 2: destructive goal -> classify + preview + confirm ---------------
Type "gitguard --offline --repo scratch-repo 'undo the last commit and discard the changes'"
Sleep 500ms
Enter
Sleep 4s

# The guardrail: we decline, so nothing is mutated in the demo.
Type "n"
Sleep 800ms
Enter
Sleep 4s