"""Counter abstraction: let a wide counter jump toward the values the rest
of the design actually reacts to, instead of counting there one cycle at a
time.

Adapted from the counter-abstraction technique in Seligman et al.'s
"Formal Verification" (Ch. 10: replace an N-bit counter with a small state
machine of its critical values, driven by a free variable), Siemens'
Verification Horizons "How to Reduce the Complexity of Formal Analysis"
(Part 4, Counter Abstraction), and the FVM complexity guide. A 32-bit
timeout counter has a sequential depth of up to 2^32 cycles, so a proof or
cover that needs it to expire is out of reach for any bounded engine, even
though nothing about the property cares about the intermediate counts.

How it is done here, and why it is sound for proofs. Every
`cnt <= cnt + 1` is rewritten to `cnt <= __cabs_inc_cnt`, where

    __cabs_inc_cnt = jump ? <the next critical value, minus 1> : cnt + 1

and `jump` is a free (`anyseq`) bit. With `jump == 0` that is exactly the
original increment, so EVERY real behavior is still possible; with
`jump == 1` the counter skips forward to one step before the next
threshold the design compares it against (one step before, not on it, so
the real +1 into the threshold still happens). The abstract design's
behaviors are therefore a superset of the real design's. That makes a
PROVEN verdict valid for the real design. A FALSIFIED verdict, or a REACHED
cover, may depend on a skip the real counter could only reach after the
full count: they're reported with that caveat, same direction as black-
boxing and cut points.

"Critical values" are found statically, from comparisons of the counter
against literals or numeric parameters (`==`, `!=`, `>=`, `>`, `<`, `<=`),
and from bit tests like `cnt[31]` (critical at 2^31). Missing a critical
value costs precision (a skip past it), never soundness, because the
real +1 path is always preserved. Only increments of the exact form
`x <= x + 1` are rewritten; loads, clears, holds, and decrements are
left alone.

Text-level and best-effort, like blackbox.py: no real parse. The jump bit
is declared `(* anyseq *) wire`, which yosys treats as a fresh free value
each cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .analyzer import RtlModule, find_hash_paren_close, _find_matching_paren, _skip_ws
from .blackbox import _find_module_span

_MAX_THRESHOLDS = 8
_LIT = r"(?:\d+'[sS]?[dDhHbBoO][0-9a-fA-F_xXzZ]+|\d+)"
_ONE = r"(?:1|\d+'[sS]?[dD]0*1|\d+'[hH]0*1|\d+'[bB]0*1)"


@dataclass
class CounterCandidate:
    signal: str
    width: int
    thresholds: List[int]
    increments: int
    observable: bool  # the counter is an output port: properties can see skipped values
    reason: str
    notes: List[str] = field(default_factory=list)


def _parse_literal(text: str, params: Dict[str, int]) -> Optional[int]:
    text = text.strip()
    if text in params:
        return params[text]
    m = re.fullmatch(r"(\d+)'[sS]?([dDhHbBoO])([0-9a-fA-F_]+)", text)
    if m:
        base = {"d": 10, "h": 16, "b": 2, "o": 8}[m.group(2).lower()]
        try:
            return int(m.group(3).replace("_", ""), base)
        except ValueError:
            return None
    if text.isdigit():
        return int(text)
    return None


def _numeric_params(body: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for m in re.finditer(
        rf"\b(?:localparam|parameter)\b[^;=]*?\b([A-Za-z_]\w*)\s*=\s*({_LIT})\s*[,;)]", body
    ):
        v = _parse_literal(m.group(2), {})
        if v is not None:
            out[m.group(1)] = v
    return out


def _paren_depth_at(body: str, pos: int) -> int:
    return body.count("(", 0, pos) - body.count(")", 0, pos)


def _find_thresholds(body: str, sig: str, params: Dict[str, int]) -> List[int]:
    found: set = set()
    s = re.escape(sig)
    operand = rf"(?:{_LIT}|[A-Za-z_]\w*)"
    for m in re.finditer(rf"\b{s}\b\s*(==|!=|>=|<=|>|<)\s*({operand})", body):
        op, rhs = m.group(1), _parse_literal(m.group(2), params)
        if rhs is None:
            continue
        if op == "<=" and _paren_depth_at(body, m.start()) == 0:
            continue  # `cnt <= 0;` at statement level is an assignment, not a comparison
        found.add({"==": rhs, "!=": rhs, ">=": rhs, ">": rhs + 1, "<": rhs, "<=": rhs + 1}[op])
    for m in re.finditer(rf"({operand})\s*(==|!=|>=|<=|>|<)\s*\b{s}\b", body):
        lhs, op = _parse_literal(m.group(1), params), m.group(2)
        if lhs is None:
            continue
        # `K op cnt` mirrors to `cnt op' K`
        found.add({"==": lhs, "!=": lhs, ">=": lhs + 1, "<=": lhs, ">": lhs, "<": lhs + 1}[op])
    for m in re.finditer(rf"\b{s}\s*\[\s*(\d+)\s*(?::\s*(\d+)\s*)?\]", body):
        found.add(1 << int(m.group(2) if m.group(2) is not None else m.group(1)))
    return sorted(k for k in found if k >= 2)


def _declared_width(body: str, sig: str) -> Optional[int]:
    m = re.search(rf"\breg\s*(?:signed\s*)?\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*{re.escape(sig)}\b", body)
    return abs(int(m.group(1)) - int(m.group(2))) + 1 if m else None


def _increment_spans(body: str, sig: str) -> List[Tuple[int, int]]:
    """(start, end) of the RHS `sig + 1` in each `sig <= sig + 1;` / `sig = sig + 1;`."""
    s = re.escape(sig)
    spans = []
    for m in re.finditer(rf"\b{s}\s*(?:<=|=)(?!=)\s*({s}\s*\+\s*{_ONE})\s*;", body):
        spans.append((m.start(1), m.end(1)))
    return spans


def find_counter_candidates(
    module: RtlModule, rtl_source: str, min_width: int = 8
) -> List[CounterCandidate]:
    start, end = _find_module_span(rtl_source, module.name)
    body = rtl_source[start:end]
    params = _numeric_params(body)
    out_names = {p.name for p in module.outputs}
    skip = {module.clock_port, module.reset_port, module.state_reg}
    cands: List[CounterCandidate] = []
    seen: set = set()
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*(?:<=|=)(?!=)\s*\1\s*\+", body):
        sig = m.group(1)
        if sig in seen or sig in skip:
            continue
        seen.add(sig)
        width = _declared_width(body, sig)
        incs = _increment_spans(body, sig)
        if width is None or width < min_width or not incs:
            continue
        thresholds = _find_thresholds(body, sig, params)
        if not thresholds:
            continue
        cands.append(CounterCandidate(
            signal=sig, width=width, thresholds=thresholds[:_MAX_THRESHOLDS], increments=len(incs),
            observable=sig in out_names,
            reason=(f"{width}-bit counter with {len(incs)} `{sig} <= {sig} + 1` increment(s) compared "
                    f"against {len(thresholds)} critical value(s): reaching them costs up to "
                    f"{thresholds[0]} cycles of sequential depth"),
            notes=(["The counter is an output port: a property on its exact value can see a skipped "
                    "count, so a FALSIFIED on such a property may be an artifact."]
                   if sig in out_names else []),
        ))
    cands.sort(key=lambda c: (c.thresholds[-1] if c.thresholds else 0, c.width), reverse=True)
    return cands


def _insertion_point(body: str) -> int:
    """Index just after the module header's terminating ';'."""
    m = re.match(r"\s*module\s+\w+\s*", body)
    pos = m.end() if m else 0
    if pos < len(body) and body[pos] == "#":
        pos = _skip_ws(body, find_hash_paren_close(body, pos) + 1)
    if pos < len(body) and body[pos] == "(":
        pos = _find_matching_paren(body, pos) + 1
    semi = body.find(";", pos)
    return semi + 1 if semi != -1 else pos


def abstract_counters(
    rtl_source: str, module_name: str, signals: List[str]
) -> Tuple[str, List[dict], List[str]]:
    """Rewrite the increments of each named counter in `module_name`.
    Returns (new_source, report, errors); a counter that can't be
    abstracted is reported in `errors`, never silently skipped.
    """
    start, end = _find_module_span(rtl_source, module_name)
    body = rtl_source[start:end]
    params = _numeric_params(body)
    report: List[dict] = []
    errors: List[str] = []
    edits: List[Tuple[int, int, str]] = []
    header_end = _insertion_point(body)

    for sig in signals:
        width = _declared_width(body, sig)
        if width is None:
            errors.append(f"'{sig}' has no `reg [N:M]` declaration with literal bounds in '{module_name}'")
            continue
        incs = _increment_spans(body, sig)
        if not incs:
            errors.append(f"'{sig}' has no `{sig} <= {sig} + 1` increment to abstract in '{module_name}'")
            continue
        thresholds = _find_thresholds(body, sig, params)[:_MAX_THRESHOLDS]
        w = width
        target = f"({sig} + {w}'d1)"
        for k in reversed(thresholds):
            target = f"(({sig} < {w}'d{k - 1}) ? {w}'d{k - 1} : {target})"
        decl_m = re.search(rf"\breg\s*(?:signed\s*)?\[\s*\d+\s*:\s*\d+\s*\]\s*(?:\w+\s*,\s*)*{re.escape(sig)}\b", body)
        decl_semi = body.find(";", decl_m.end()) if decl_m else -1
        # Declarations go right after the counter's own declaration (a wire
        # referencing `cnt` before `reg cnt` is declared would not elaborate),
        # or after the module header when the counter is declared in the port list.
        ins_at = max(header_end, decl_semi + 1 if decl_semi != -1 else header_end)
        edits.append((ins_at, ins_at, "\n" + (
            f"    // counter abstraction of `{sig}` (see counter_abstract.py): free skip toward "
            f"critical value(s) {thresholds or 'none found'}\n"
            f"    (* anyseq *) wire __cabs_jump_{sig};\n"
            f"    wire [{w - 1}:0] __cabs_inc_{sig} = __cabs_jump_{sig} ? {target} : ({sig} + {w}'d1);"
        )))
        for s_, e_ in incs:
            edits.append((s_, e_, f"__cabs_inc_{sig}"))
        report.append({"signal": sig, "width": width, "thresholds": thresholds, "increments_rewritten": len(incs)})

    if errors and not report:
        return rtl_source, report, errors
    for s_, e_, text in sorted(edits, key=lambda t: (t[0], t[1]), reverse=True):
        body = body[:s_] + text + body[e_:]
    return rtl_source[:start] + body + rtl_source[end:], report, errors
