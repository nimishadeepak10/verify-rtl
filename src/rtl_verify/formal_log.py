"""Persistent, append-only log of every formal-tab attempt (suggest,
convert, run) — Phase 2's p2-8. Local JSONL file, one line per event, so
"what did I actually try, and what happened" survives across sessions
instead of vanishing the moment the browser tab closes.

Deliberately does not log full RTL/spec text (could be large or sensitive)
— only shape (line counts, module name, property counts, verdicts).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = _ROOT / "logs"
LOG_PATH = LOG_DIR / "formal_runs.jsonl"


def log_event(kind: str, detail: dict[str, Any]) -> None:
    """Append one event. Never raises — logging must not break a request."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), "kind": kind, **detail}
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def read_recent(limit: int = 30) -> list[dict[str, Any]]:
    """Most recent events first."""
    if not LOG_PATH.is_file():
        return []
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    events.reverse()
    return events
