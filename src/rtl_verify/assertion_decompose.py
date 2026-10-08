"""Assertion decomposition: split one compound assert into independent,
simpler asserts whose conjunction is logically identical to the original.

Adapted from the property-simplification technique in Seligman et al.'s
"Formal Verification" (Ch. 10: replace `(a || b) |-> c` with
`a |-> c` and `b |-> c`; replace `d |-> (e && f)` with `d |-> e` and
`d |-> f`) and the FVM complexity guide's "assertion decomposition".
Smaller properties converge more reliably, and a failure points at the
exact sub-claim that broke instead of the whole compound one.

This project writes properties as `!(GUARD) || (CONCLUSION)` (see
vacuity.py), so the two identities are applied in that shape:

  GUARD split:       !(a || b) || c   ==  (!(a) || c) && (!(b) || c)
  CONCLUSION split:  !(g) || (x && y) ==  (!(g) || x) && (!(g) || y)

Both are exact Boolean equivalences, not approximations, so
`PROVEN iff every part is PROVEN` and `FALSIFIED iff any part is
FALSIFIED` hold exactly. A bare top-level `a && b` (no guard) splits the
same way. Splitting happens only at the TOP level of the expression
(outside any parentheses/brackets/braces); anything else is returned
unchanged rather than guessed at.

Deliberately text-only: no solver is involved in producing the parts.
"""

from __future__ import annotations

from typing import List


def _split_top_level(expr: str, op: str) -> List[str]:
    """Split `expr` on `op` ('||' or '&&') occurring at nesting depth 0,
    never matching the single-char `|`/`&` operators.
    """
    parts: List[str] = []
    depth = 0
    start = 0
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and expr.startswith(op, i):
            parts.append(expr[start:i].strip())
            i += len(op)
            start = i
            continue
        i += 1
    parts.append(expr[start:].strip())
    return [p for p in parts if p]


def _strip_outer_parens(s: str) -> str:
    s = s.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    return s  # the first '(' closes before the end: not a full wrap
        s = s[1:-1].strip()
    return s


def _match_paren(text: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def decompose_assertion(expr: str) -> List[str]:
    """Return the list of equivalent sub-expressions, or `[expr]`
    unchanged if there is nothing safe to split.
    """
    text = expr.strip().rstrip(";").strip()
    if not text:
        return [expr]

    # Shape 1: !(GUARD) || REST
    if text.startswith("!("):
        close = _match_paren(text, 1)
        rest = text[close + 1:].strip() if close != -1 else ""
        if close != -1 and rest.startswith("||"):
            guard = text[2:close].strip()
            conclusion = rest[2:].strip()
            guards = _split_top_level(guard, "||")
            top_or = _split_top_level(conclusion, "||")
            # A conclusion that is itself a top-level OR must stay whole.
            conclusions = [conclusion]
            if len(top_or) == 1:
                inner = _strip_outer_parens(conclusion)
                conclusions = _split_top_level(inner, "&&") if len(_split_top_level(inner, "||")) == 1 else [conclusion]
            parts = [
                f"!({g}) || ({c})"
                for g in guards
                for c in conclusions
            ]
            return parts if len(parts) > 1 else [expr]

    # Shape 2: bare top-level conjunction
    inner = _strip_outer_parens(text)
    if len(_split_top_level(inner, "||")) == 1:
        conj = _split_top_level(inner, "&&")
        if len(conj) > 1:
            return conj

    return [expr]
