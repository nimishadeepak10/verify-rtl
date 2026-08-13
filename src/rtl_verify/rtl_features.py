"""Detect RTL / language features that need a SystemVerilog-capable simulator."""

from __future__ import annotations

import re

from .analyzer import _strip_comments

_SV_SIM_MARKERS = [
    r"\balways_comb\b",
    r"\balways_ff\b",
    r"\balways_latch\b",
    r"\blogic\b",
    r"\binterface\b",
    r"\bpackage\b",
    r"\bimport\b",
    r"\btypedef\b",
    r"\benum\b",
    r"\bstruct\b",
    r"\$clog2\s*\(",
    r"'\{",
    r"\[\s*\w+\s*-\s*\d+\s*:\s*\d+\s*\]\s*\[",  # unpacked array port
    r"\bparameter\s+int\b",
    r"#\s*\(",  # parameter port list
]


def needs_systemverilog_simulator(rtl: str, language: str = "") -> bool:
    lang = (language or "").lower()
    if lang in ("systemverilog", "uvm", "sv"):
        return True
    clean = _strip_comments(rtl or "")
    return any(re.search(pat, clean, re.IGNORECASE) for pat in _SV_SIM_MARKERS)


def dut_source_extension(rtl: str, language: str = "") -> str:
    return ".sv" if needs_systemverilog_simulator(rtl, language) else ".v"


def detect_rtl_language(rtl: str) -> str:
    """Infer whether RTL is best treated as Verilog or SystemVerilog."""
    return "systemverilog" if needs_systemverilog_simulator(rtl, "") else "verilog"


def resolve_tb_language(rtl: str, requested: str = "systemverilog") -> str:
    """Pick testbench language from RTL content (user UVM choice is preserved)."""
    if (requested or "").lower() == "uvm":
        return "uvm"
    return detect_rtl_language(rtl)
