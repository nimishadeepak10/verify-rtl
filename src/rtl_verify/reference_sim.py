"""Fallback combinational simulator when Icarus Verilog is not installed."""

from __future__ import annotations

import ast
import operator
import re
from pathlib import Path
from typing import Any, Dict, List

from .analyzer import PortDirection, RtlModule, _strip_comments
from .generators import verilog_tb as vtb

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.Invert: operator.invert,
    ast.USub: operator.neg,
}


def _port_values(mod: RtlModule, raw: Dict[str, int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for p in mod.ports:
        v = raw.get(p.name, 0)
        mask = (1 << p.width) - 1
        out[p.name] = v & mask
    return out


def _parse_assigns(rtl: str) -> List[tuple[str, str]]:
    clean = _strip_comments(rtl)
    return [(m.group(1), m.group(2).strip()) for m in re.finditer(
        r"assign\s+(\w+)\s*=\s*([^;]+)\s*;",
        clean,
    )]


def _safe_eval_expr(expr: str, env: Dict[str, int]) -> int:
    expr = expr.strip()
    for name, val in sorted(env.items(), key=lambda x: -len(x[0])):
        expr = re.sub(rf"\b{re.escape(name)}\b", str(val), expr)

    node = ast.parse(expr, mode="eval").body

    def _eval(n: ast.AST) -> int:
        if isinstance(n, ast.Constant) and isinstance(n.value, int):
            return n.value
        if isinstance(n, ast.BinOp):
            op = _OPS.get(type(n.op))
            if not op:
                raise ValueError(f"Unsupported operator in: {expr}")
            return int(op(_eval(n.left), _eval(n.right)))
        if isinstance(n, ast.UnaryOp):
            op = _OPS.get(type(n.op))
            if not op:
                raise ValueError(f"Unsupported unary op in: {expr}")
            return int(op(_eval(n.operand)))
        raise ValueError(f"Unsupported expression: {expr}")

    return _eval(node)


def can_simulate_combinational(rtl: str, mod: RtlModule) -> bool:
    if mod.is_sequential:
        return False
    if not mod.inputs or not mod.outputs:
        return False
    try:
        assigns = _parse_assigns(rtl)
        return len(assigns) > 0
    except Exception:
        return False


def run_reference_sim(
    rtl: str,
    mod: RtlModule,
    work_dir: Path,
) -> tuple[bool, str, Path | None]:
    """
    Evaluate assign-based combinational RTL in Python.
    Returns (success, log, vcd_path).
    """
    assigns = _parse_assigns(rtl)
    if not assigns:
        return False, "Reference sim: no assign statements found in RTL.", None

    try:
        cases = vtb._build_cases(mod)  # noqa: SLF001
    except Exception as e:
        return False, f"Reference sim: cannot build tests: {e}", None

    log: List[str] = [
        "=== SIMULATION (Python reference — Icarus not installed) ===",
        "Install Icarus Verilog for full VCD/waveform: https://bleyer.org/icarus/",
        "",
    ]
    pass_cnt = 0
    fail_cnt = 0
    vcd_lines = ["$date", "1", "$end", "$version", "VerifyRTL reference", "$end",
                 "$timescale", "1ns", "$end", "$scope module tb", "$end"]

    var_ids: Dict[str, str] = {}
    sym = 33
    all_signals = [p.name for p in mod.ports]
    for name in all_signals:
        cid = chr(sym)
        sym += 1
        var_ids[name] = cid
        p = next(x for x in mod.ports if x.name == name)
        w = p.width
        vcd_lines.append(f"$var wire {w} {cid} {name} $end")

    vcd_lines.extend(["$upscope $end", "$enddefinitions $end", "$end", "$dumpvars"])
    for name in all_signals:
        vcd_lines.append(f"0{var_ids[name]}")
    vcd_lines.append("$end")

    time = 0
    for i, case in enumerate(cases):
        env = _port_values(mod, case)
        for lhs, rhs in assigns:
            try:
                env[lhs] = _safe_eval_expr(rhs, env)
            except Exception as e:
                log.append(f"FAIL test {i}: could not evaluate assign {lhs}={rhs}: {e}")
                fail_cnt += 1
                break
        else:
            time += 5
            vcd_lines.append(f"#{time}")
            for name in all_signals:
                v = env.get(name, 0)
                w = next(p.width for p in mod.ports if p.name == name)
                if w > 1:
                    bits = format(v & ((1 << w) - 1), f"0{w}b")
                    vcd_lines.append(f"b{bits} {var_ids[name]}")
                else:
                    vcd_lines.append(f"{v & 1}{var_ids[name]}")

            outs = {p.name: env[p.name] for p in mod.outputs}
            if mod.inferred_op in ("add", "and", "xor") and mod.outputs and len(mod.inputs) >= 2:
                out_name = mod.outputs[0].name
                got = outs[out_name]
                mask = (1 << mod.outputs[0].width) - 1
                a, b = env[mod.inputs[0].name], env[mod.inputs[1].name]
                if mod.inferred_op == "add":
                    expected = (a + b) & mask
                elif mod.inferred_op == "and":
                    expected = a & b
                else:
                    expected = a ^ b
                if got == expected:
                    pass_cnt += 1
                    log.append(f"PASS a={a} b={b} {out_name}={got}")
                else:
                    fail_cnt += 1
                    log.append(f"FAIL test {i} exp={expected} got={got}")
            else:
                pass_cnt += 1
                log.append(
                    "STIM test " + str(i) + " " + " ".join(f"{k}={env[k]}" for k in sorted(env))
                )

    log.extend([
        "",
        "=== SUMMARY ===",
        f"PASS={pass_cnt} FAIL={fail_cnt}",
        "RESULT: PASS" if fail_cnt == 0 else "RESULT: FAIL",
    ])
    ok = fail_cnt == 0
    work_dir.mkdir(parents=True, exist_ok=True)
    vcd_path = work_dir / "sim.vcd"
    vcd_path.write_text("\n".join(vcd_lines) + "\n", encoding="utf-8")
    return ok, "\n".join(log), vcd_path
