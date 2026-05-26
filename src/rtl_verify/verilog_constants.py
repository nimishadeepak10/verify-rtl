"""Parse Verilog numeric literals (reusable across golden model and coverage)."""

from __future__ import annotations

import re
from typing import Optional, Tuple

# Sized: 8'b1010, 4'hA, 3'd5, 3'o7; optional sign: -3'sd5
_RE_SIZED = re.compile(
    r"(?P<sign>-)?\s*(?:(?P<width>\d+)\s*)?'(?P<base>[bhdosBHDOS])\s*(?P<digits>[0-9a-fA-FxXzZ?_]+)",
    re.IGNORECASE,
)
_RE_PLAIN = re.compile(r"^-?\d+$")


def parse_verilog_constant(s: str) -> Optional[int]:
    """
    Parse a Verilog constant to an unsigned bit pattern (int).
    Returns None if not a recognized literal.
    """
    s = s.strip()
    if not s:
        return None
    if _RE_PLAIN.fullmatch(s):
        return int(s, 10)

    m = _RE_SIZED.fullmatch(s)
    if not m:
        return None

    sign = m.group("sign")
    width = int(m.group("width") or "32")
    base = m.group("base").lower()
    digits = m.group("digits").replace("_", "")

    if base == "b":
        if not digits or set(digits) <= set("01"):
            val = int(digits, 2) if digits else 0
        else:
            # Wildcard patterns (casez labels) — store as int with ? -> 0 for parsing
            val = 0
            for i, ch in enumerate(reversed(digits)):
                if ch in "01":
                    val |= int(ch) << i
    elif base == "h":
        val = int(digits, 16) if digits else 0
    elif base == "o":
        val = int(digits, 8) if digits else 0
    else:
        val = int(digits, 10) if digits else 0

    mask = (1 << width) - 1
    val = val & mask
    if sign and base == "d":
        if val & (1 << (width - 1)):
            val = val - (1 << width)
        return val
    return val


def parse_verilog_constant_bits(s: str) -> Optional[Tuple[int, int, str]]:
    """
    For casez/casex labels: return (width, value_bits, pattern_string).
    pattern_string uses original digits for wildcard matching.
    """
    s = s.strip()
    m = _RE_SIZED.fullmatch(s)
    if not m:
        if _RE_PLAIN.fullmatch(s):
            v = int(s, 10)
            w = max(1, v.bit_length())
            return w, v, format(v, f"0{w}b")
        return None
    width = int(m.group("width") or "32")
    base = m.group("base").lower()
    digits = m.group("digits").replace("_", "")
    if base != "b":
        v = parse_verilog_constant(s)
        if v is None:
            return None
        return width, v & ((1 << width) - 1), format(v & ((1 << width) - 1), f"0{width}b")
    # Binary with ? z x
    pat = digits.lower().replace("z", "?").replace("x", "?")
    val = 0
    for i, ch in enumerate(reversed(pat)):
        if ch == "1":
            val |= 1 << i
    return width, val & ((1 << width) - 1), pat


def casez_match(selector: int, sel_width: int, pattern: str) -> bool:
    """True if selector matches casez/casex pattern (top-down caller)."""
    info = parse_verilog_constant_bits(pattern)
    if info is None:
        lit = parse_verilog_constant(pattern)
        return lit is not None and (selector & ((1 << sel_width) - 1)) == lit
    width, _val, pat = info
    w = min(width, sel_width)
    sel_bits = format(selector & ((1 << sel_width) - 1), f"0{sel_width}b")[-w:]
    pat_bits = pat[-w:].rjust(w, "0")
    for sb, pb in zip(sel_bits, pat_bits):
        if pb in "?":
            continue
        if sb != pb:
            return False
    return True
