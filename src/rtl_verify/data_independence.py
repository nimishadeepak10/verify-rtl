"""Data independence: prove, statically, that a design's control behavior
does not depend on the VALUES flowing through its datapath, so the datapath
can be shrunk without changing what the control does.

Adapted from the data-independence discussion in Seligman et al.'s "Formal
Verification" (Ch. 10) and the FVM complexity guide's "Data Independence"
entry: if correctness depends on the transport/control logic and not on
the payload (a FIFO at 3 bits vs. 64 bits), the width can be reduced. Both
sources attach the same condition: "sound only when data values don't
affect control behavior". The earlier parameter-reduction feature
(param_reduce.py) refused to touch widths for exactly that reason, because
the condition was not checked. This module checks it.

How: a taint (information-flow) analysis over the module's own text.
Seeds are the named data signals; taint propagates through every
assignment (`assign`, `<=`, `=`, and `wire x = ...;`) to whatever is
computed from it, including memories (an array becomes tainted when any
element is written from tainted data). Then every CONTROL SINK is
examined: the condition of an `if`/`while`/`for`, a `case` selector, the
select part of a `?:`, and any array or bit-select index. If a tainted
signal reaches a control sink, the data affects control and the status is
DEPENDENT, with the exact line. A tainted signal that is only ever copied,
stored, or sent to an output leaves the status INDEPENDENT.

Anything the analysis cannot see through gives UNKNOWN, never INDEPENDENT:
tainted data connected to a submodule port, or passed as an argument to a
user function/task. The check is text-level and conservative in the
direction that matters (it over-reports flow); it can wrongly say
DEPENDENT, but a clean INDEPENDENT means no flow into a control sink was
found by these rules.

What INDEPENDENT buys, precisely. If data cannot influence control, the
sequence of control states is the same at every data width, so a property
that mentions NO data signal gets an EXACT result at a reduced width, in
both directions. A property that does mention data (an integrity check) is
a different matter: the standard data-independence argument covers
designs that only MOVE data (copy, select, concatenate, store) checked by
properties that use data only by equality. So the analysis also records
whether the design COMPUTES on data (any arithmetic, logic or comparison
operator applied to a tainted value, `transport_only`). If it does, a
reduced-width result for a data-mentioning property is not evidence for the
real width (a multiplier bug that only shows in the upper bits would
simply vanish), and such properties are refused. Properties that apply
arithmetic or magnitude comparison to data are refused regardless.

A width can only be reduced where a parameter controls it, and only a
parameter that sizes data signals and nothing else (no clean signal's
range, no use outside declaration ranges and replications) is offered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .analyzer import PortDirection, RtlModule
from .blackbox import _find_module_span
from .param_reduce import ParamReduction, list_parameters

_KEYWORDS = {
    "if", "else", "begin", "end", "case", "casez", "casex", "endcase", "default", "assign", "always",
    "posedge", "negedge", "or", "and", "not", "wire", "reg", "logic", "input", "output", "inout",
    "integer", "for", "while", "repeat", "initial", "module", "endmodule", "parameter", "localparam",
    "genvar", "generate", "endgenerate", "function", "endfunction", "signed", "unsigned",
}
_IDX = r"\[(?:[^\[\]]|\[[^\]]*\])*\]"  # one bracket pair, allowing one nested level
_ASSIGN = re.compile(r"\b([A-Za-z_]\w*)\s*((?:" + _IDX + r"\s*)*)(<=|=)(?!=)\s*([^;]+);")
_DECL_ASSIGN = re.compile(r"\b(?:wire|logic|reg)\b\s*(?:signed\s*)?(?:\[[^\]]*\]\s*)?([A-Za-z_]\w*)\s*=\s*([^;]+);")
_STMT_START = re.compile(r"(?:;|\bbegin\b|\belse\b|:|\)|\bassign\b|^)\s*$")


@dataclass
class FlowViolation:
    signal: str
    sink: str
    line: int
    snippet: str


@dataclass
class DataIndependenceReport:
    status: str  # INDEPENDENT | DEPENDENT | UNKNOWN | ERROR
    data_signals: List[str]
    tainted: List[str]
    violations: List[FlowViolation] = field(default_factory=list)
    unknowns: List[str] = field(default_factory=list)
    note: str = ""
    # True when tainted values are only copied/selected/concatenated, never combined by an
    # arithmetic, logic or comparison operator (see module docstring)
    transport_only: bool = True
    computations: List[FlowViolation] = field(default_factory=list)


def _strip_comments(src: str) -> str:
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j == -1 else j
            out[i:j] = " " * (j - i)
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _idents(expr: str) -> Set[str]:
    return {t for t in re.findall(r"(?<![\$\w'])[A-Za-z_]\w*", expr) if t not in _KEYWORDS}


def _match(text: str, open_idx: int, o: str = "(", c: str = ")") -> int:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == o:
            depth += 1
        elif text[i] == c:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _ternary_conditions(expr: str) -> List[str]:
    """Condition text of every `?` at every nesting depth in `expr`."""
    conds: List[str] = []

    def scan(seg: str) -> None:
        depth, seg_start = 0, 0
        i = 0
        while i < len(seg):
            ch = seg[i]
            if ch in "([{":
                close = _match(seg, i, ch, {"(": ")", "[": "]", "{": "}"}[ch])
                if close == -1:
                    return
                scan(seg[i + 1:close])
                i = close
            elif ch == "?":
                conds.append(seg[seg_start:i])
                seg_start = i + 1
            elif ch == ":":
                seg_start = i + 1
            i += 1

    scan(expr)
    return conds


def _line(src: str, idx: int) -> int:
    return src.count("\n", 0, idx) + 1


def _snip(src: str, a: int, b: int) -> str:
    return re.sub(r"\s+", " ", src[a:b]).strip()[:90]


def _assignments(body: str) -> List[Tuple[str, str, str, int]]:
    """(lhs_base, lhs_index_text, rhs, offset) for each real assignment."""
    out: List[Tuple[str, str, str, int]] = []
    for m in _ASSIGN.finditer(body):
        before = body[:m.start()]
        if not _STMT_START.search(before[-40:]) and not before.rstrip().endswith((";", ")", "begin", "else", ":")):
            continue
        name = m.group(1)
        if name in _KEYWORDS:
            continue
        out.append((name, m.group(2), m.group(4), m.start()))
    for m in _DECL_ASSIGN.finditer(body):
        out.append((m.group(1), "", m.group(2), m.start()))
    return out


def analyze_data_independence(
    module: RtlModule, rtl_source: str, data_signals: List[str]
) -> DataIndependenceReport:
    try:
        start, end = _find_module_span(rtl_source, module.name)
    except ValueError as e:
        return DataIndependenceReport("ERROR", data_signals, [], note=str(e))
    raw = rtl_source[start:end]
    body = _strip_comments(raw)

    declared = {p.name for p in module.ports} | set(re.findall(r"\b(?:reg|wire|logic)\b\s*(?:signed\s*)?(?:\[[^\]]*\]\s*)?(\w+)", body))
    bad = [s for s in data_signals if s not in declared]
    if bad or not data_signals:
        return DataIndependenceReport(
            "ERROR", data_signals, [],
            note=(f"Not declared in '{module.name}': {bad}" if bad else "No data signals were named."))

    assigns = _assignments(body)
    tainted: Set[str] = set(data_signals)
    changed = True
    while changed:
        changed = False
        for lhs, _idx, rhs, _off in assigns:
            if lhs not in tainted and _idents(rhs) & tainted:
                tainted.add(lhs)
                changed = True

    violations: List[FlowViolation] = []
    unknowns: List[str] = []

    def check(expr: str, kind: str, offset: int) -> None:
        for sig in sorted(_idents(expr) & tainted):
            violations.append(FlowViolation(sig, kind, _line(raw, offset), _snip(body, max(0, offset - 4), offset + len(expr) + 6)))

    for m in re.finditer(r"\b(if|while|for|repeat)\s*\(", body):
        close = _match(body, m.end() - 1)
        if close != -1:
            check(body[m.end():close], f"{m.group(1)} condition", m.end())
    for m in re.finditer(r"\b(casez|casex|case)\s*\(", body):
        close = _match(body, m.end() - 1)
        if close != -1:
            check(body[m.end():close], "case selector", m.end())
    for lhs, idx, rhs, off in assigns:
        for cond in _ternary_conditions(rhs):
            check(cond, "ternary select", off)
        if idx:
            check(idx, "write index/address", off)
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\[", body):
        if m.group(1) in _KEYWORDS:
            continue  # a declaration range such as `reg [W-1:0]`, not an index
        close = _match(body, m.end() - 1, "[", "]")
        if close == -1:
            continue
        idx_expr = body[m.end():close]
        if re.fullmatch(r"\s*(?:\d+|\d*'[sS]?[bBdDhHoO][0-9a-fA-F_]+)\s*", idx_expr):
            continue
        check(idx_expr, "array/bit index", m.end())

    # things the analysis cannot see through -> UNKNOWN, never INDEPENDENT
    for m in re.finditer(r"\.\s*(\w+)\s*\(([^()]*)\)", body):
        if _idents(m.group(2)) & tainted:
            unknowns.append(f"tainted signal(s) {sorted(_idents(m.group(2)) & tainted)} connect to submodule port '.{m.group(1)}' (line {_line(raw, m.start())}); the submodule is not analyzed")
    for m in re.finditer(r"(?<![\$\w.])([A-Za-z_]\w*)\s*\(([^;]*)\)", body):
        name = m.group(1)
        if name in _KEYWORDS or name in {"posedge", "negedge"}:
            continue
        if re.search(rf"\b(?:function|task)\b[^;]*\b{name}\b", body) and _idents(m.group(2)) & tainted:
            unknowns.append(f"tainted data passed to user function/task '{name}' (line {_line(raw, m.start())}); its body is not analyzed")

    computations: List[FlowViolation] = []
    for lhs, _idx, rhs, off in assigns:
        stripped = rhs
        for cond in _ternary_conditions(rhs):
            stripped = stripped.replace(cond, " ")
        stripped = re.sub(r"\[(?:[^\[\]]|\[[^\]]*\])*\]", " ", stripped)
        if _idents(stripped) & tainted and re.search(r"[-+*/%&|^~<>!=]", stripped):
            computations.append(FlowViolation(lhs, "computation on data", _line(raw, off), _snip(body, off, off + 70)))

    uniq: Dict[Tuple[str, str, int], FlowViolation] = {}
    for v in violations:
        uniq[(v.signal, v.sink, v.line)] = v
    violations = list(uniq.values())
    violations.sort(key=lambda v: v.line)

    if violations:
        status = "DEPENDENT"
        note = (f"Data reaches control: {len(violations)} flow(s) into a control sink. Shrinking the "
                "data width could change the control behavior, so a width reduction would be unsound.")
    elif unknowns:
        status = "UNKNOWN"
        note = "No direct flow into a control sink was found, but some paths cannot be analyzed (see unknowns)."
    else:
        status = "INDEPENDENT"
        note = ("No tainted signal reaches an if/case/loop condition, a ternary select, or an index "
                "(text-level information-flow analysis).")
    return DataIndependenceReport(
        status, list(data_signals), sorted(tainted), violations, unknowns, note,
        transport_only=not computations, computations=computations)


def guess_data_signals(module: RtlModule) -> List[str]:
    """Input ports whose names say payload. A hint only: the analysis, not
    the name, decides whether reduction is allowed."""
    pat = re.compile(r"(data|payload|wdata|din|dat\b|word)", re.IGNORECASE)
    return [p.name for p in module.ports
            if p.direction == PortDirection.INPUT and pat.search(p.name)
            and p.name not in (module.clock_port, module.reset_port)]


def pure_data_width_parameters(
    module: RtlModule, rtl_source: str, tainted: List[str]
) -> List[Tuple[str, int]]:
    """Literal parameters that size ONLY data-carrying signals (and appear
    nowhere else but declaration ranges and `{PARAM{...}}` replications)."""
    start, end = _find_module_span(rtl_source, module.name)
    body = _strip_comments(rtl_source[start:end])
    tainted_set = set(tainted)
    out: List[Tuple[str, int]] = []
    for name, value, where in list_parameters(rtl_source, module.name):
        if where != "module_default":
            continue
        occ = [m.start() for m in re.finditer(rf"\b{re.escape(name)}\b", body)]
        # occurrences that are the parameter's own definition
        defs = [m.start(1) for m in re.finditer(rf"\b(?:parameter|localparam)\b[^;,)]*?\b({re.escape(name)})\s*=", body)]
        uses = [o for o in occ if o not in defs]
        if not uses:
            continue
        ok_uses, clean_users = 0, False
        for m in re.finditer(r"\[([^\]]*)\]\s*((?:\w+\s*(?:\[[^\]]*\])?\s*,\s*)*\w+)", body):
            if re.search(rf"\b{re.escape(name)}\b", m.group(1)):
                names = [n for n in re.findall(r"\b[A-Za-z_]\w*\b", re.sub(r"\[[^\]]*\]", "", m.group(2)))
                         if n not in _KEYWORDS]  # the greedy name list can run into the next `output`/`input`
                if all(n in tainted_set for n in names):
                    ok_uses += len(re.findall(rf"\b{re.escape(name)}\b", m.group(1)))
                else:
                    clean_users = True
        for m in re.finditer(rf"\{{\s*{re.escape(name)}\s*\{{", body):
            ok_uses += 1
        if not clean_users and ok_uses == len(uses):
            out.append((name, value))
    return out


def recommend_data_width_reductions(
    module: RtlModule, rtl_source: str, data_signals: List[str], target: int = 4, min_value: int = 8
) -> Tuple[DataIndependenceReport, List[ParamReduction]]:
    report = analyze_data_independence(module, rtl_source, data_signals)
    if report.status != "INDEPENDENT":
        return report, []
    recs = [
        ParamReduction(
            name=n, original=v, proposed=min(target, v), kind="width", where="module_default",
            reason=(f"'{n}' sizes only data-carrying signals and data was shown not to reach control, "
                    "so the control behavior is identical at the reduced width."),
        )
        for n, v in pure_data_width_parameters(module, rtl_source, report.tainted) if v >= min_value
    ]
    return report, recs


def property_data_use(expr: str, tainted: List[str]) -> Tuple[bool, bool]:
    """(mentions_data, uses_arithmetic_or_magnitude_on_data)."""
    ids = _idents(expr) & set(tainted)
    if not ids:
        return False, False
    stripped = re.sub(r"==|!=|&&|\|\|", " ", expr)
    return True, bool(re.search(r"[+\-*/%<>]", stripped))
