"""Fallback combinational simulator when Icarus Verilog is not installed."""

from __future__ import annotations

from pathlib import Path
from typing import List

from .analyzer import RtlModule
from .combinational_model import (
    can_evaluate_combinational,
    evaluate_case,
    expected_outputs,
)
from .generators import verilog_tb as vtb


def can_simulate_combinational(rtl: str, mod: RtlModule) -> bool:
    return can_evaluate_combinational(rtl, mod)


def run_reference_sim(
    rtl: str,
    mod: RtlModule,
    work_dir: Path,
) -> tuple[bool, str, Path | None]:
    """
    Evaluate assign-based combinational RTL in Python.
    Returns (success, log, vcd_path).
    """
    if not can_evaluate_combinational(rtl, mod):
        return False, "Reference sim: no supported assign-based combinational RTL found.", None

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
    vcd_lines = [
        "$date",
        "1",
        "$end",
        "$version",
        "VerifyRTL reference",
        "$end",
        "$timescale",
        "1ns",
        "$end",
        "$scope module tb",
        "$end",
    ]

    var_ids: dict[str, str] = {}
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
        try:
            env = evaluate_case(rtl, mod, case)
            golden = expected_outputs(rtl, mod, case)
        except Exception as e:
            log.append(f"FAIL test {i}: {e}")
            fail_cnt += 1
            continue

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

        out_parts = [f"{p.name}={golden[p.name]}" for p in mod.outputs]
        in_parts = [f"{p.name}={env[p.name]}" for p in mod.inputs]
        pass_cnt += 1
        log.append(f"PASS test {i} " + " ".join(in_parts) + " -> " + " ".join(out_parts))

    log.extend(
        [
            "",
            "=== SUMMARY ===",
            f"PASS={pass_cnt} FAIL={fail_cnt}",
            "RESULT: PASS" if fail_cnt == 0 else "RESULT: FAIL",
        ]
    )
    ok = fail_cnt == 0
    work_dir.mkdir(parents=True, exist_ok=True)
    vcd_path = work_dir / "sim.vcd"
    vcd_path.write_text("\n".join(vcd_lines) + "\n", encoding="utf-8")
    return ok, "\n".join(log), vcd_path
