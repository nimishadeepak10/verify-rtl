"""Evaluate combinational always @(*) blocks (case / if-else) for golden reference."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .analyzer import RtlModule, _strip_comments
from .combinational_model import _eval_verilog_expr, _port_values
from .verilog_constants import casez_match, parse_verilog_constant

_RE_ALWAYS_COMB = re.compile(
    r"always\s*@\s*\(\s*\*\s*\)\s*begin(.*?)end\b",
    re.IGNORECASE | re.DOTALL,
)
_RE_ALWAYS_COMB2 = re.compile(
    r"always_comb\s*begin(.*?)end\b",
    re.IGNORECASE | re.DOTALL,
)
_RE_CASEZ_HEAD = re.compile(r"casez\s*\(\s*([^)]+)\s*\)", re.IGNORECASE)
_RE_CASEX_HEAD = re.compile(r"casex\s*\(\s*([^)]+)\s*\)", re.IGNORECASE)
_RE_CASE_HEAD = re.compile(r"\bcase(?![zx])\s*\(\s*([^)]+)\s*\)", re.IGNORECASE)
_RE_ENDCASE = re.compile(r"\bendcase\b", re.IGNORECASE)
_RE_ASSIGN = re.compile(r"(\w+)\s*=\s*([^;]+)\s*;?")


def extract_always_blocks(rtl: str) -> List[str]:
    clean = _strip_comments(rtl)
    blocks: List[str] = []
    for m in re.finditer(
        r"always\s*@\s*\(\s*\*\s*\)\s*begin",
        clean,
        re.IGNORECASE,
    ):
        body, _ = _extract_balanced_block(clean, m.end())
        if body.strip():
            blocks.append(body.strip())
    for m in re.finditer(r"always_comb\s*begin", clean, re.IGNORECASE):
        body, _ = _extract_balanced_block(clean, m.end())
        if body.strip():
            blocks.append(body.strip())
    return blocks


def _extract_balanced_block(text: str, start: int) -> Tuple[str, int]:
    """Return inner text between begin at start-5..start and matching end."""
    depth = 1
    i = start
    while i < len(text):
        if re.match(r"\bbegin\b", text[i:], re.IGNORECASE):
            depth += 1
            i += 5
            continue
        if re.match(r"\bend\b", text[i:], re.IGNORECASE):
            depth -= 1
            if depth == 0:
                return text[start:i], i + 3
            i += 3
            continue
        i += 1
    return text[start:], len(text)


def can_evaluate_always(rtl: str, mod: RtlModule) -> bool:
    if mod.is_sequential or not mod.outputs:
        return False
    blocks = extract_always_blocks(rtl)
    if not blocks:
        return False
    try:
        case = {p.name: 0 for p in mod.inputs}
        evaluate_always(rtl, mod, case)
        return True
    except Exception:
        return False


def evaluate_always(
    rtl: str,
    mod: RtlModule,
    case: Dict[str, int],
    env: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    if env is None:
        env = _port_values(mod, case)
    else:
        env = dict(env)
    widths = {p.name: p.width for p in mod.ports}
    blocks = extract_always_blocks(rtl)
    if not blocks:
        raise ValueError("no always @(*) blocks")
    for body in blocks:
        _exec_block(body, env, widths)
    return env


def _case_kind_and_selector(rest: str) -> Tuple[Optional[str], Optional[str], int]:
    for kind, pat in (
        ("casez", _RE_CASEZ_HEAD),
        ("casex", _RE_CASEX_HEAD),
        ("case", _RE_CASE_HEAD),
    ):
        m = pat.match(rest)
        if m:
            return kind, m.group(1).strip(), m.end()
    return None, None, 0


def _exec_block(body: str, env: Dict[str, int], widths: Dict[str, int]) -> None:
    rest = body.strip()
    while rest:
        rest = rest.lstrip()
        if not rest:
            break

        kind, selector, head_end = _case_kind_and_selector(rest)
        if kind and selector is not None:
            end_m = _RE_ENDCASE.search(rest)
            if not end_m:
                raise ValueError("case without endcase")
            case_body = rest[head_end : end_m.start()]
            _eval_case(selector, case_body, env, widths, kind)
            rest = rest[end_m.end() :].lstrip()
            continue

        m_if = re.match(r"if\s*\(", rest, re.IGNORECASE)
        if m_if:
            n = _eval_if_chain(rest, env, widths)
            rest = rest[n:].lstrip()
            continue

        m_as = _RE_ASSIGN.match(rest)
        if m_as:
            tgt, rhs = m_as.group(1), m_as.group(2).strip()
            w = widths.get(tgt, 32)
            env[tgt] = _eval_verilog_expr(rhs, env, widths) & ((1 << w) - 1)
            rest = rest[m_as.end() :].lstrip()
            continue

        raise ValueError(f"unsupported always statement: {rest[:80]}")


def _eval_case(
    selector: str,
    case_body: str,
    env: Dict[str, int],
    widths: Dict[str, int],
    kind: str = "case",
) -> None:
    sel_val = _eval_verilog_expr(selector, env, widths)
    sel_w = widths.get(selector.strip(), max(1, sel_val.bit_length()))
    arms = _parse_case_arms(case_body)
    for label, stmt in arms:
        if label.lower() == "default":
            continue
        matched = False
        if kind in ("casez", "casex"):
            matched = casez_match(sel_val, sel_w, label)
        else:
            lit = parse_verilog_constant(label)
            matched = lit is not None and lit == (sel_val & ((1 << sel_w) - 1))
        if matched:
            _exec_case_stmt(stmt, env, widths)
            return
    for label, stmt in arms:
        if label.lower() == "default":
            _exec_case_stmt(stmt, env, widths)
            return


def _parse_case_arms(case_body: str) -> List[Tuple[str, str]]:
    arms: List[Tuple[str, str]] = []
    # Labels: default, sized binary with ?/z/x, plain decimal
    pat = (
        r"(?:^|[\n;])\s*"
        r"(default|(?:\d+\s*)?'[bhdosBHD][\w?xzXZ_]+|\d+)"
        r"\s*:\s*"
    )
    positions = [(m.start(), m.group(1).strip()) for m in re.finditer(pat, case_body, re.IGNORECASE)]
    for i, (pos, label) in enumerate(positions):
        start = case_body.find(":", pos) + 1
        end = positions[i + 1][0] if i + 1 < len(positions) else len(case_body)
        stmt = case_body[start:end].strip()
        stmt = re.sub(r"\bbegin\b", "", stmt, flags=re.IGNORECASE)
        stmt = re.sub(r"\bend\b", "", stmt, flags=re.IGNORECASE).strip()
        arms.append((label, stmt))
    return arms


def _exec_case_stmt(stmt: str, env: Dict[str, int], widths: Dict[str, int]) -> None:
    stmt = stmt.strip()
    if not stmt:
        return
    for m in _RE_ASSIGN.finditer(stmt):
        tgt, rhs = m.group(1), m.group(2).strip()
        w = widths.get(tgt, 32)
        env[tgt] = _eval_verilog_expr(rhs, env, widths) & ((1 << w) - 1)


def _eval_if_chain(text: str, env: Dict[str, int], widths: Dict[str, int]) -> int:
    taken = False
    for part in text.split(";"):
        stmt = part.strip()
        if not stmt:
            continue
        m_if = re.match(
            r"if\s*\(\s*([^)]+)\s*\)\s*(\w+)\s*=\s*(.+)$",
            stmt,
            re.IGNORECASE,
        )
        m_elif = re.match(
            r"else\s+if\s*\(\s*([^)]+)\s*\)\s*(\w+)\s*=\s*(.+)$",
            stmt,
            re.IGNORECASE,
        )
        m_else = re.match(r"else\s+(\w+)\s*=\s*(.+)$", stmt, re.IGNORECASE)
        m = m_if or m_elif
        if m:
            if not taken and _eval_verilog_expr(m.group(1).strip(), env, widths):
                w = widths.get(m.group(2), 32)
                env[m.group(2)] = (
                    _eval_verilog_expr(m.group(3).strip(), env, widths) & ((1 << w) - 1)
                )
                taken = True
            continue
        if m_else:
            if not taken:
                w = widths.get(m_else.group(1), 32)
                env[m_else.group(1)] = (
                    _eval_verilog_expr(m_else.group(2).strip(), env, widths)
                    & ((1 << w) - 1)
                )
                taken = True
            break
    return len(text)


def infer_case_arm_ops(case_body: str) -> List[str]:
    ops: List[str] = []
    for _label, stmt in _parse_case_arms(case_body):
        s = stmt.replace(" ", "")
        if "?" in s and ":" in s:
            ops.append("ternary")
        elif "+" in s:
            ops.append("add")
        elif "-" in s:
            ops.append("sub")
        elif "&" in s and "|" not in s:
            ops.append("and")
        elif "|" in s and "^" not in s:
            ops.append("or")
        elif "^" in s:
            ops.append("xor")
        elif "~" in s:
            ops.append("not")
        elif "<<" in s:
            ops.append("shl")
        elif ">>" in s:
            ops.append("shr")
        elif "{" in s:
            ops.append("concat")
        else:
            ops.append("op")
    return ops
