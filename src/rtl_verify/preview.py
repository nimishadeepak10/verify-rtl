"""Build pre-simulation test plan / DUT info report."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .analyzer import PortDirection, RtlModule, analyze_rtl, _strip_comments
from .generators.base import TbLanguage
from .generators import verilog_tb as vtb
from .reference_sim import can_simulate_combinational
from .simulator import find_iverilog


def list_modules(rtl: str) -> List[str]:
    clean = _strip_comments(rtl)
    return [m.group(1) for m in re.finditer(r"\bmodule\s+(\w+)\s*[#(]?", clean)]


def build_test_preview(
    rtl: str,
    language: TbLanguage = TbLanguage.SYSTEMVERILOG,
    top_module: Optional[str] = None,
) -> Dict[str, Any]:
    mod = analyze_rtl(rtl, top_module=top_module)
    modules = list_modules(rtl)

    try:
        cases = vtb._build_cases(mod)  # noqa: SLF001
        test_count = len(cases)
        strategy = "exhaustive" if test_count <= 512 else "random-directed"
    except Exception:
        cases = []
        test_count = 0
        strategy = "manual"

    self_check = mod.inferred_op in ("add", "and", "xor") and mod.inputs and mod.outputs
    stim_space = 1
    for p in mod.inputs:
        stim_space *= min((1 << p.width), 256)

    lang_label = {
        TbLanguage.VERILOG: "Verilog-2001 testbench",
        TbLanguage.SYSTEMVERILOG: "SystemVerilog testbench",
        TbLanguage.UVM: "UVM environment (sequence, driver, monitor)",
    }[language]

    has_iverilog = bool(find_iverilog())
    ref_ok = can_simulate_combinational(rtl, mod)
    if language == TbLanguage.UVM:
        sim_backend = "Questa / VCS / Xcelium (UVM)"
        sim_runs = False
    elif has_iverilog:
        sim_backend = "Icarus Verilog (iverilog + vvp)"
        sim_runs = True
    elif ref_ok:
        sim_backend = "Python reference sim (install Icarus for full Verilog)"
        sim_runs = True
    else:
        sim_backend = "None — install Icarus Verilog"
        sim_runs = False

    warnings: List[str] = []
    if not mod.inputs:
        warnings.append("No input ports — directed stimulus cannot be auto-generated.")
    if not mod.outputs:
        warnings.append("No output ports — results cannot be checked automatically.")
    if not self_check:
        warnings.append("Golden model unknown — simulation will log outputs (monitor mode).")
    if language == TbLanguage.UVM:
        warnings.append("UVM will not run in-browser; download TB and use your UVM simulator.")

    checklist = [
        {"id": "rtl", "label": "RTL parsed", "ok": bool(mod.ports)},
        {"id": "ports", "label": "Port list extracted", "ok": len(mod.ports) > 0},
        {"id": "inputs", "label": "Stimulus inputs present", "ok": len(mod.inputs) > 0},
        {"id": "outputs", "label": "Observable outputs present", "ok": len(mod.outputs) > 0},
        {"id": "golden", "label": "Self-checking reference available", "ok": self_check},
        {"id": "sim", "label": "Simulator available for selected language", "ok": sim_runs},
    ]

    return {
        "module_name": mod.name,
        "modules_in_file": modules,
        "ports": [
            {
                "name": p.name,
                "direction": p.direction.value,
                "width": p.width,
                "range": p.range_str() or "[0:0]",
            }
            for p in mod.ports
        ],
        "input_ports": [p.name for p in mod.inputs],
        "output_ports": [p.name for p in mod.outputs],
        "inferred_operation": mod.inferred_op,
        "inferred_operation_label": _op_label(mod.inferred_op),
        "language": language.value,
        "language_label": lang_label,
        "simulator": sim_backend,
        "sim_will_run": sim_runs,
        "test_strategy": strategy,
        "test_count": test_count,
        "stimulus_space_estimate": stim_space,
        "self_checking": self_check,
        "tb_top": f"tb_{mod.name}",
        "warnings": warnings,
        "checklist": checklist,
        "ready_to_run": len(mod.inputs) > 0 and len(mod.outputs) > 0,
        "summary_lines": _summary_lines(mod, language, test_count, strategy, self_check),
    }


def _op_label(op: Optional[str]) -> str:
    return {
        "add": "Addition (a + b)",
        "and": "Bitwise AND",
        "xor": "Bitwise XOR",
        "binary_op": "Two-input combinational logic",
        None: "Not inferred — monitor-only",
    }.get(op, op or "Unknown")


def _summary_lines(
    mod: RtlModule,
    language: TbLanguage,
    test_count: int,
    strategy: str,
    self_check: bool,
) -> List[str]:
    lines = [
        f"DUT: {mod.name}",
        f"Ports: {len(mod.ports)} ({len(mod.inputs)} in, {len(mod.outputs)} out)",
        f"Verification: {language.value.upper()}",
        f"Tests planned: {test_count} ({strategy})",
        f"Checking: {'self-checking' if self_check else 'monitor-only'}",
    ]
    return lines
