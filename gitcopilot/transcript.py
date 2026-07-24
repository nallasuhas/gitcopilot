"""Transcript + replay log.

Every run appends a JSON-lines record of the goal and each step (command,
classification, whether it executed, output, and any denial). This gives you a
reviewable history of which commands solved which task, and a `replay` command
that re-prints a past session without touching the repo.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field


@dataclass
class TranscriptStep:
    argv: list[str]
    tier: str
    rationale: str
    executed: bool
    exit_code: int | None
    output_preview: str
    denied_reason: str = ""


@dataclass
class TranscriptRecord:
    goal: str
    planner: str
    cwd: str
    started_at: str  # ISO string; passed in by caller (no clock in core)
    steps: list[TranscriptStep] = field(default_factory=list)
    final_summary: str = ""


class Transcript:
    def __init__(self, path: str | None):
        self.path = path

    def write(self, record: TranscriptRecord) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_record_to_dict(record)) + "\n")

    def load(self) -> list[TranscriptRecord]:
        if not self.path or not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                d["steps"] = [TranscriptStep(**s) for s in d.get("steps", [])]
                out.append(TranscriptRecord(**d))
        return out


def _record_to_dict(record: TranscriptRecord) -> dict:
    d = asdict(record)
    return d
