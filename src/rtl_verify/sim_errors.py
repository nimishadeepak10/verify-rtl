"""Extract human-readable errors from synthesis and simulation logs."""

from __future__ import annotations

import re
from typing import List


_ERROR_PATTERNS = (
    re.compile(r"^ERROR:\s*", re.IGNORECASE),
    re.compile(r"^error:\s*", re.IGNORECASE),
    re.compile(r"syntax error", re.IGNORECASE),
    re.compile(r"compilation failed", re.IGNORECASE),
    re.compile(r"unable to compile", re.IGNORECASE),
    re.compile(r"undefined module", re.IGNORECASE),
    re.compile(r"not synthesizable", re.IGNORECASE),
)


def extract_errors(synth_log: str = "", sim_log: str = "", synth_ok: bool = True) -> List[str]:
    """Return deduplicated error lines from tool output."""
    seen: set[str] = set()
    out: List[str] = []

    def add_line(line: str) -> None:
        s = line.strip()
        if not s or s in seen:
            return
        if any(p.search(s) for p in _ERROR_PATTERNS):
            seen.add(s)
            out.append(s)

    for blob in (synth_log or "", sim_log or ""):
        for line in blob.splitlines():
            add_line(line)

    if not synth_ok and not out:
        out.append("RTL failed Vivado synthesis check — see synthesis log.")

    return out[:64]
