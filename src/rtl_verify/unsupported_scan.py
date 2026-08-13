"""Scan RTL for constructs the golden model cannot interpret (simulation may still run)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from .analyzer import _strip_comments


@dataclass
class UnsupportedConstruct:
    name: str
    line_no: int
    detail: str = ""

    def message(self) -> str:
        if self.detail:
            return f"golden model limitation — {self.name} at line {self.line_no} ({self.detail})"
        return f"golden model limitation — {self.name} at line {self.line_no}"


@dataclass
class RtlScanResult:
    unsupported_constructs: List[UnsupportedConstruct] = field(default_factory=list)

    @property
    def is_fully_supported(self) -> bool:
        return len(self.unsupported_constructs) == 0


def scan_rtl_for_unsupported(rtl: str) -> RtlScanResult:
    """Constructs that block automatic expected-value checking (not HDL simulation)."""
    clean = _strip_comments(rtl)
    lines = clean.splitlines()
    found: List[UnsupportedConstruct] = []

    # Patterns that prevent Python golden-model evaluation — simulators handle these fine.
    patterns = [
        (r"\bfor\s*\(", "for loop"),
        (r"\bwhile\s*\(", "while loop"),
        (r"\brepeat\s*\(", "repeat loop"),
        (r"\bfunction\s+", "function definition"),
        (r"\btask\s+", "task definition"),
        (r"\bgenerate\b", "generate block"),
        (r"\binterface\b", "interface"),
        (r"\bpackage\b", "package"),
        (r"\bimport\s+", "import statement"),
        (r"\bdpi-c\b", "DPI import"),
    ]

    for line_no, raw in enumerate(lines, start=1):
        s = raw.strip()
        if not s or s.startswith("//"):
            continue
        for pat, name in patterns:
            if re.search(pat, s, re.IGNORECASE):
                found.append(UnsupportedConstruct(name=name, line_no=line_no))
                break

    return RtlScanResult(unsupported_constructs=found)


def format_unsupported_report(items: List[UnsupportedConstruct]) -> str:
    if not items:
        return ""
    lines = ["Note: automatic checking unavailable (simulation still runs):"]
    for u in items:
        lines.append(f"        {u.message().replace('golden model limitation — ', '')}")
    return "\n".join(lines)
