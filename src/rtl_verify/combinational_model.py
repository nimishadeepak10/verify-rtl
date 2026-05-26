"""Golden reference for assign-based combinational RTL."""

from __future__ import annotations

import ast
import operator
import re
from typing import Dict, List, Optional, Tuple, Union

from .analyzer import RtlModule, _strip_comments
from .unsupported_scan import RtlScanResult, scan_rtl_for_unsupported
from .verilog_constants import parse_verilog_constant

AssignItem = Union[Tuple[str, str, str], Tuple[str, List[str], str]]

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

_RE_ASSIGN_CONCAT = re.compile(
    r"assign\s*\{([^}]+)\}\s*=\s*([^;]+)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_RE_ASSIGN_SIMPLE = re.compile(
    r"assign\s+(\w+)\s*=\s*([^;]+)\s*;",
    re.IGNORECASE | re.DOTALL,
)

_scan_cache: Dict[int, RtlScanResult] = {}


def get_rtl_scan(rtl: str) -> RtlScanResult:
    key = hash(rtl)
    if key not in _scan_cache:
        _scan_cache[key] = scan_rtl_for_unsupported(rtl)
    return _scan_cache[key]


def parse_assigns(rtl: str) -> List[AssignItem]:
    clean = _strip_comments(rtl)
    items: List[AssignItem] = []
    for m in _RE_ASSIGN_CONCAT.finditer(clean):
        parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
        if parts:
            items.append(("concat", parts, m.group(2).strip()))
    for m in _RE_ASSIGN_SIMPLE.finditer(clean):
        items.append(("simple", m.group(1), m.group(2).strip()))
    return items


def can_self_check(rtl: str, mod: RtlModule) -> bool:
    if not get_rtl_scan(rtl).is_fully_supported:
        return False
    return can_evaluate_combinational(rtl, mod)


def can_evaluate_combinational(rtl: str, mod: RtlModule) -> bool:
    if mod.is_sequential:
        return False
    if not mod.inputs or not mod.outputs:
        return False
    if not get_rtl_scan(rtl).is_fully_supported:
        return False
    try:
        cases = _sample_case(mod)
        evaluate_case(rtl, mod, cases[0])
        return True
    except Exception:
        return False


def evaluate_case(rtl: str, mod: RtlModule, case: Dict[str, int]) -> Dict[str, int]:
    from .always_model import extract_always_blocks, evaluate_always

    env = _port_values(mod, case)
    widths = {p.name: p.width for p in mod.ports}
    assigns = parse_assigns(rtl)
    if assigns:
        for item in assigns:
            kind = item[0]
            if kind == "concat":
                names: List[str] = item[1]  # type: ignore[index]
                rhs: str = item[2]  # type: ignore[index]
                val = _eval_verilog_expr(rhs, env, widths)
                total_w = sum(widths.get(n, 1) for n in names)
                pos = total_w
                for name in names:
                    w = widths.get(name, 1)
                    pos -= w
                    mask = (1 << w) - 1
                    env[name] = (val >> pos) & mask
            else:
                lhs: str = item[1]  # type: ignore[index]
                rhs = item[2]  # type: ignore[index]
                w = widths.get(lhs, 32)
                env[lhs] = _eval_verilog_expr(rhs, env, widths) & ((1 << w) - 1)
    if extract_always_blocks(rtl):
        env = evaluate_always(rtl, mod, case, env=env)
    elif not assigns:
        raise ValueError("no supported combinational RTL structure")
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


def _to_signed(val: int, width: int) -> int:
    mask = (1 << width) - 1
    v = val & mask
    if v & (1 << (width - 1)):
        return v - (1 << width)
    return v


def _eval_slice(expr: str, env: Dict[str, int], widths: Dict[str, int]) -> Optional[int]:
    m = re.fullmatch(r"(\w+)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]", expr.strip())
    if not m:
        return None
    sig, msb_s, lsb_s = m.group(1), int(m.group(2)), int(m.group(3))
    if sig not in env:
        return None
    val = env[sig]
    w = widths.get(sig, max(msb_s, lsb_s) + 1)
    val &= (1 << w) - 1
    if msb_s < lsb_s:
        msb_s, lsb_s = lsb_s, msb_s
    width = msb_s - lsb_s + 1
    return (val >> lsb_s) & ((1 << width) - 1)


def _eval_concat_rhs(expr: str, env: Dict[str, int], widths: Dict[str, int]) -> int:
    inner = expr.strip()[1:-1]
    parts = [p.strip() for p in inner.split(",")]
    result = 0
    shift = 0
    for part in reversed(parts):
        if part.startswith("{"):
            v = _eval_concat_rhs(part, env, widths)
            part_w = v.bit_length() or 1
        else:
            sl = _eval_slice(part, env, widths)
            if sl is not None:
                v = sl
                m = re.search(r"\[(\d+)\s*:\s*(\d+)\]", part)
                part_w = abs(int(m.group(1)) - int(m.group(2))) + 1 if m else max(1, v.bit_length())
            else:
                v = _eval_verilog_expr(part, env, widths)
                part_w = widths.get(part, max(1, v.bit_length()))
        result |= (v & ((1 << part_w) - 1)) << shift
        shift += part_w
    return result


def _reduce_op(op: str, name: str, env: Dict[str, int], widths: Dict[str, int]) -> str:
    if name not in env:
        raise ValueError(f"unknown signal {name}")
    w = widths.get(name, 32)
    v = env[name] & ((1 << w) - 1)
    bits = format(v, f"0{w}b")
    if op in ("^", "~^"):
        x = bits.count("1") % 2
        r = x if op == "^" else (1 - x)
    elif op in ("&", "~&"):
        r = 1 if bits and "0" not in bits else 0
        if op == "~&":
            r = 1 - r
    elif op in ("|", "~|"):
        r = 1 if "1" in bits else 0
        if op == "~|":
            r = 1 - r
    else:
        raise ValueError(f"unknown reduction {op}")
    return str(r)


def _apply_reductions(expr: str, env: Dict[str, int], widths: Dict[str, int]) -> str:
    """Unary reduction only (^a, &b, |c, ~^d) — not binary XOR/AND/OR."""

    def repl_unary(m: re.Match[str]) -> str:
        prefix, op, name = m.group(1), m.group(2), m.group(3)
        return prefix + _reduce_op(op, name, env, widths)

    prev = None
    while prev != expr:
        prev = expr
        expr = re.sub(
            r"(^|[=(,]\s*)(~?[\^&|])\s*(\w+)\b",
            repl_unary,
            expr,
        )
    return expr


def _apply_signed_wrappers(expr: str, env: Dict[str, int], widths: Dict[str, int]) -> str:
    """Replace innermost $signed/$unsigned; keep signed ints for comparisons."""
    while "$signed" in expr or "$unsigned" in expr:
        m = re.search(r"\$signed\s*\(", expr)
        if not m:
            m = re.search(r"\$unsigned\s*\(", expr)
        if not m:
            break
        is_signed = expr[m.start() : m.start() + 7] == "$signed"
        paren_start = expr.index("(", m.start())
        inner, close = _match_paren_content(expr, paren_start)
        if inner is None:
            break
        inner_s = inner.strip()
        if inner_s.startswith("{"):
            val = _eval_concat_rhs(inner_s, env, widths)
            w = max(val.bit_length(), 1)
        elif "[" in inner_s:
            val = _eval_slice(inner_s, env, widths) or 0
            wm = re.search(r"\[(\d+)\s*:\s*(\d+)\]", inner_s)
            w = abs(int(wm.group(1)) - int(wm.group(2))) + 1 if wm else 8
        else:
            val = env.get(inner_s, 0)
            w = widths.get(inner_s, 8)
        if is_signed:
            val = _to_signed(val, w)
        else:
            val = val & ((1 << w) - 1)
        expr = expr[: m.start()] + str(val) + expr[close + 1 :]
    return expr


def _match_paren_content(s: str, start: int) -> Tuple[Optional[str], int]:
    if start >= len(s) or s[start] != "(":
        return None, start
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return s[start + 1 : i], i
    return None, start


def _normalize_verilog_numbers(expr: str) -> str:
    def repl(m: re.Match[str]) -> str:
        lit = m.group(0)
        v = parse_verilog_constant(lit)
        return str(v if v is not None else 0)

    expr = re.sub(
        r"(?:(?:-)?\d+\s*)?'[bhdosBHDOS][0-9a-fA-FxXzZ?_]+",
        repl,
        expr,
    )
    expr = re.sub(r"\b(\d+)\s*'d\s*(\d+)\b", r"\2", expr)
    return expr


def _find_prev_colon(expr: str, before: int) -> int:
    depth = 0
    for i in range(before - 1, -1, -1):
        c = expr[i]
        if c == ")":
            depth += 1
        elif c == "(":
            depth -= 1
        elif c == ":" and depth == 0:
            return i
    return -1


def _eval_verilog_expr(
    expr: str,
    env: Dict[str, int],
    widths: Optional[Dict[str, int]] = None,
) -> int:
    widths = widths or {k: max(1, v.bit_length()) for k, v in env.items()}
    expr = expr.strip()
    if expr.startswith("{") and expr.endswith("}"):
        return _eval_concat_rhs(expr, env, widths)
    expr = _apply_signed_wrappers(expr, env, widths)
    expr = _apply_reductions(expr, env, widths)
    expr = re.sub(r"\s+", " ", _normalize_verilog_numbers(expr))
    for name, val in sorted(env.items(), key=lambda x: -len(x[0])):
        expr = re.sub(rf"\b{re.escape(name)}\b", str(val), expr)
    while "?" in expr:
        cpos = -1
        depth = 0
        for i in range(len(expr) - 1, -1, -1):
            c = expr[i]
            if c == ")":
                depth += 1
            elif c == "(":
                depth -= 1
            elif c == ":" and depth == 0:
                cpos = i
                break
        if cpos < 0:
            break
        qpos = -1
        depth = 0
        for i in range(cpos - 1, -1, -1):
            c = expr[i]
            if c == ")":
                depth += 1
            elif c == "(":
                depth -= 1
            elif c == "?" and depth == 0:
                qpos = i
                break
        if qpos < 0:
            break
        prev_colon = _find_prev_colon(expr, qpos)
        cond_start = 0 if prev_colon < 0 else prev_colon + 1
        cond = expr[cond_start:qpos].strip()
        true_br = expr[qpos + 1 : cpos].strip()
        false_br = expr[cpos + 1 :].strip()
        pick = true_br if _safe_eval_expr(cond, env, widths) else false_br
        val = _eval_verilog_expr(pick, env, widths)
        expr = expr[:cond_start] + str(val)
    return _safe_eval_expr(expr, env, widths)


def _safe_eval_expr(
    expr: str,
    env: Dict[str, int] | None = None,
    widths: Optional[Dict[str, int]] = None,
) -> int:
    env = env or {}
    widths = widths or {}
    for name, val in sorted(env.items(), key=lambda x: -len(x[0])):
        expr = re.sub(rf"\b{re.escape(name)}\b", str(val), expr)
    expr = expr.replace("===", "==").replace("!==", "!=")
    # Python 'and'/'or' from Verilog &&/||
    expr = re.sub(r"\s*&&\s*", " and ", expr)
    expr = re.sub(r"\s*\|\|\s*", " or ", expr)
    expr = re.sub(r"!(?!=)", " not ", expr)
    node = ast.parse(expr.strip(), mode="eval").body

    def _signed_val(v: int, w: int) -> int:
        return _to_signed(v, w) if w else v

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
        if isinstance(n, ast.BoolOp):
            vals = [_eval(v) for v in n.values]
            if isinstance(n.op, ast.And):
                return 1 if all(vals) else 0
            if isinstance(n.op, ast.Or):
                return 1 if any(vals) else 0
        if isinstance(n, ast.Name):
            if n.id in env:
                return int(env[n.id])
            raise ValueError(f"unknown identifier in: {expr}")
        if isinstance(n, ast.Compare):
            left = _eval(n.left)
            ok = True
            for op, comp in zip(n.ops, n.comparators):
                right = _eval(comp)
                if isinstance(op, ast.Eq):
                    ok = ok and (left == right)
                elif isinstance(op, ast.NotEq):
                    ok = ok and (left != right)
                elif isinstance(op, ast.Lt):
                    ok = ok and (left < right)
                elif isinstance(op, ast.LtE):
                    ok = ok and (left <= right)
                elif isinstance(op, ast.Gt):
                    ok = ok and (left > right)
                elif isinstance(op, ast.GtE):
                    ok = ok and (left >= right)
                else:
                    raise ValueError(f"unsupported compare in: {expr}")
                left = right
            return 1 if ok else 0
        raise ValueError(f"unsupported expression: {expr}")

    return _eval(node)


def _parse_verilog_literal(label: str) -> Optional[int]:
    return parse_verilog_constant(label)
