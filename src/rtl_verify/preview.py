"""Build pre-simulation test plan / DUT info report."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .analyzer import PortDirection, RtlModule, analyze_rtl, _strip_comments
from .backends.registry import ALL_BACKENDS, auto_select, get_backend
from .generators.base import TbLanguage


def list_modules(rtl: str) -> List[str]:
    clean = _strip_comments(rtl)
    return [m.group(1) for m in re.finditer(r"\bmodule\s+(\w+)\s*[#(]?", clean)]


def _resolve_simulator_label(
    language: TbLanguage,
    rtl: str,
    mod: RtlModule,
    backend: Optional[str],
) -> tuple[str, bool]:
    if language == TbLanguage.UVM:
        return "Questa / VCS / Xcelium (UVM)", False

    chosen = None
    if backend and backend.strip():
        chosen = get_backend(backend.strip().lower())
        if chosen is None:
            return f"Unknown backend: {backend}", False
        if not chosen.is_available():
            return f"{chosen.display_name} — not installed", False
        if chosen.name == "reference" and mod.is_sequential:
            return f"{chosen.display_name} — sequential RTL needs Icarus", False
        ver = chosen.version()
        label = chosen.display_name + (f" ({ver})" if ver else "")
        return label, True

    chosen = auto_select(language.value, is_sequential=mod.is_sequential, rtl_source=rtl)
    if chosen:
        ver = chosen.version()
        label = chosen.display_name + (f" ({ver})" if ver else "")
        if chosen.name == "reference" and mod.is_sequential:
            return label + " — sequential RTL needs Icarus", False
        return label, True

    names = ", ".join(b.display_name for b in ALL_BACKENDS)
    return f"None available (checked: {names})", False


def build_test_preview(
    rtl: str,
    language: TbLanguage = TbLanguage.SYSTEMVERILOG,
    top_module: Optional[str] = None,
    backend: Optional[str] = None,
) -> Dict[str, Any]:
    mod = analyze_rtl(rtl, top_module=top_module)
    modules = list_modules(rtl)

    try:
        stim_ports = mod.data_inputs if mod.is_sequential else mod.inputs
        stim_ports = [p for p in stim_ports if not getattr(p, "is_unpacked_array", False)]
        stim_steps = min(16, max(8, len(stim_ports) * 4 or 8))
        test_count = stim_steps
        if mod.is_sequential:
            strategy = "sequential waveform stimulus"
        else:
            strategy = "combinational waveform stimulus"
    except Exception:
        test_count = 8
        strategy = "waveform stimulus"

    self_check = False
    stim_ports = mod.data_inputs if mod.is_sequential else mod.inputs
    stim_space = 1
    for p in stim_ports:
        stim_space *= min((1 << p.width), 256)

    lang_label = {
        TbLanguage.VERILOG: "Verilog-2001 testbench",
        TbLanguage.SYSTEMVERILOG: "SystemVerilog testbench",
        TbLanguage.UVM: "UVM environment (sequence, driver, monitor)",
    }[language]

    sim_backend, sim_runs = _resolve_simulator_label(language, rtl, mod, backend)

    warnings: List[str] = []
    if not mod.inputs:
        warnings.append("No input ports — testbench will observe outputs only.")
    if not mod.outputs:
        warnings.append("No output ports — cannot generate a useful testbench.")
    if not mod.is_sequential:
        warnings.append(
            "Behavior is verified via synthesis check + waveform simulation "
            "(no per-test PASS/FAIL — inspect waveforms)."
        )
    elif mod.is_sequential:
        warnings.append("Sequential designs use clock/reset stimulus — verify behavior in waveforms.")
    if language == TbLanguage.UVM:
        warnings.append("UVM will not run in-browser; download TB and use your UVM simulator.")
    if mod.is_sequential and not mod.clock_port:
        warnings.append("Sequential logic detected but no clock port (clk/clock/ck) found.")
    if mod.is_sequential and mod.state_reg:
        warnings.append(
            f"FSM detected: state register '{mod.state_reg}', states: {', '.join(mod.states)}."
        )
    if backend == "reference" and mod.is_sequential:
        warnings.append("Reference backend cannot simulate sequential designs.")
    elif mod.is_sequential:
        icarus = get_backend("icarus")
        if icarus is None or not icarus.is_available():
            warnings.append("Sequential designs require Icarus Verilog (or another HDL simulator).")
    backend_name = backend.strip().lower() if backend and backend.strip() else None
    if backend_name:
        b = get_backend(backend_name)
        selected_backend = backend_name if b else None
    else:
        sel = auto_select(language.value, is_sequential=mod.is_sequential, rtl_source=rtl)
        selected_backend = sel.name if sel else None

    checklist = [
        {"id": "rtl", "label": "RTL parsed", "ok": bool(mod.ports)},
        {"id": "ports", "label": "Port list extracted", "ok": len(mod.ports) > 0},
        {"id": "inputs", "label": "Stimulus inputs present", "ok": len(mod.inputs) > 0},
        {"id": "outputs", "label": "Observable outputs present", "ok": len(mod.outputs) > 0},
        {"id": "golden", "label": "Waveform verification (no golden model)", "ok": True},
        {"id": "sim", "label": "Simulator available for selected language", "ok": sim_runs},
        {
            "id": "sequential",
            "label": "Clock/reset protocol" if mod.is_sequential else "Combinational (no clock)",
            "ok": (not mod.is_sequential) or bool(mod.clock_port),
        },
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
        "backend": selected_backend,
        "test_strategy": strategy,
        "test_count": test_count,
        "stimulus_space_estimate": stim_space,
        "self_checking": self_check,
        "tb_top": f"tb_{mod.name}",
        "warnings": warnings,
        "checklist": checklist,
        "ready_to_run": len(mod.outputs) > 0
        and sim_runs
        and ((mod.is_sequential and bool(mod.clock_port)) or not mod.is_sequential),
        "is_sequential": mod.is_sequential,
        "clock_port": mod.clock_port,
        "reset_port": mod.reset_port,
        "reset_active_low": mod.reset_active_low,
        "state_reg": mod.state_reg,
        "states": mod.states,
        "data_input_ports": [p.name for p in mod.data_inputs],
        "summary_lines": _summary_lines(mod, language, test_count, strategy, self_check),
    }


def _op_label(op: Optional[str]) -> str:
    return {
        "add": "Addition (a + b)",
        "and": "Bitwise AND",
        "or": "Bitwise OR",
        "xor": "Bitwise XOR",
        "sub": "Subtraction (a - b)",
        "not": "Bitwise NOT",
        "binary_op": "Two-input combinational logic",
        None: "Derived from assign statements when available",
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
        f"Type: {'sequential' if mod.is_sequential else 'combinational'}",
    ]
    if mod.clock_port:
        rst = mod.reset_port or "(none)"
        pol = "active-low" if mod.reset_active_low else "active-high"
        lines.append(f"Clock: {mod.clock_port}, Reset: {rst} ({pol})")
    if mod.state_reg:
        lines.append(f"FSM: {mod.state_reg} → {', '.join(mod.states)}")
    lines.extend([
        f"Verification: {language.value.upper()}",
        f"Testbench: {strategy} ({test_count} input patterns)",
        "Checking: waveform review (no auto PASS/FAIL)",
    ])
    return lines
