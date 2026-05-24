"""Golden reference for assign-based combinational RTL."""

from __future__ import annotations

import ast
import operator
import re
from typing import Dict, List, Optional, Tuple

from .analyzer import PortDirection, RtlModule, _strip_comments

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.Invert: operator.invert,
    ast.USub: operator.neg,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
}


def parse_assigns(rtl: str) -> List[Tuple[str, str]]:
    clean = _strip_comments(rtl)
    return [
        (m.group(1), m.group(2).strip())
        for m in re.finditer(r"assign\s+(\w+)\s*=\s*([^;]+)\s*;", clean)
    ]


def can_evaluate_combinational(rtl: str, mod: RtlModule) -> bool:
    if mod.is_sequential:
        return False
    if not mod.inputs or not mod.outputs:
        return False
    try:
        assigns = parse_assigns(rtl)
        if not assigns:
            return False
        cases = _sample_case(mod)
        evaluate_case(rtl, mod, cases[0])
        return True
    except Exception:
        return False


def evaluate_case(rtl: str, mod: RtlModule, case: Dict[str, int]) -> Dict[str, int]:
    """Apply stimulus and evaluate assign chain; returns all port values."""
    assigns = parse_assigns(rtl)
    if not assigns:
        raise ValueError("no assign statements in RTL")
    env = _port_values(mod, case)
    for lhs, rhs in assigns:
        env[lhs] = _safe_eval_expr(rhs, env)
    return env


def expected_outputs(rtl: str, mod: RtlModule, case: Dict[str, int]) -> Dict[str, int]:
    env = evaluate_case(rtl, mod, case)
    mask_by_name = {p.name: (1 << p.width) - 1 for p in mod.ports}
    return {
        p.name: env[p.name] & mask_by_name[p.name]
        for p in mod.outputs
    }


def verilog_literal(value: int, width: int) -> str:
    mask = (1 << width) - 1
    v = value & mask
    if width <= 1:
        return f"1'b{v}"
    return f"{width}'d{v}"


def _sample_case(mod: RtlModule) -> List[Dict[str, int]]:
    ins = mod.inputs
    if not ins:
        return [{}]
    return [{p.name: 0 for p in ins}]


def _port_values(mod: RtlModule, raw: Dict[str, int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for p in mod.ports:
        mask = (1 << p.width) - 1
        out[p.name] = raw.get(p.name, 0) & mask
    return out


def _normalize_verilog_numbers(expr: str) -> str:
    def repl(m: re.Match[str]) -> str:
        width = int(m.group(1)) if m.group(1) else 32
        base = m.group(2).lower()
        digits = m.group(3)
        if base == "b":
            val = int(digits.replace("_", ""), 2) if digits else 0
        elif base == "h":
            val = int(digits.replace("_", ""), 16) if digits else 0
        elif base == "d":
            val = int(digits.replace("_", ""), 10) if digits else 0
        else:
            val = int(digits, 10)
        return str(val & ((1 << width) - 1))

    expr = re.sub(
        r"(?:(\d+)\s*)?'([bhdBHD])\s*([0-9a-fA-FxXzZ?_]+)",
        repl,
        expr,
    )
    expr = re.sub(r"\b(\d+)\s*'d\s*(\d+)\b", r"\2", expr)
    return expr


def _safe_eval_expr(expr: str, env: Dict[str, int]) -> int:
    expr = _normalize_verilog_numbers(expr.strip())
    for name, val in sorted(env.items(), key=lambda x: -len(x[0])):
        expr = re.sub(rf"\b{re.escape(name)}\b", str(val), expr)

    node = ast.parse(expr, mode="eval").body

    def _eval(n: ast.AST) -> int:
        if isinstance(n, ast.Constant):
            if isinstance(n.value, bool):
                return int(n.value)
            if isinstance(n.value, int):
                return n.value
        if isinstance(n, ast.BinOp):
            op = _OPS.get(type(n.op))
            if not op:
                raise ValueError(f"unsupported operator in: {expr}")
            return int(op(_eval(n.left), _eval(n.right)))
        if isinstance(n, ast.UnaryOp):
            op = _OPS.get(type(n.op))
            if not op:
                raise ValueError(f"unsupported unary op in: {expr}")
            return int(op(_eval(n.operand)))
        raise ValueError(f"unsupported expression: {expr}")

    return _eval(node)
