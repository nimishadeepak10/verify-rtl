"""End-to-end: RTL in → testbench → simulate → reports."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .analyzer import RtlModule, analyze_rtl
from .combinational_model import can_evaluate_combinational
from .backends.registry import (
    auto_select,
    get_backend,
    missing_backend_message,
)
from .generators.base import TbLanguage, generate_testbench
from .waveform import vcd_to_html, vcd_to_text, write_dut_info
from .waveform import load_module_info, vcd_to_json
from .coverage import (
    CoverageReport,
    FsmCoverage,
    build_env_timeline_from_vcd_signals,
    compute_fsm_coverage,
    compute_overall_percent,
    compute_toggle_coverage,
)
from .rtl_interpreter import CoverageInterpreter


@dataclass
class VerificationResult:
    module: RtlModule
    language: TbLanguage
    testbench: str
    sim_log: str
    text_report: str
    waveform_text: str
    waveform_html: str
    success: bool
    work_dir: Path
    vcd_path: Optional[Path]
    uvm_note: str = ""
    status: str = "fail"  # pass | fail | sim_missing | tb_only
    simulator: str = ""
    backend_used: str = ""
    backend_version: str = ""
    coverage: CoverageReport | None = None


def run_verification(
    rtl_source: str,
    language: TbLanguage = TbLanguage.SYSTEMVERILOG,
    top_module: Optional[str] = None,
    work_dir: Optional[Path] = None,
    backend: Optional[str] = None,
) -> VerificationResult:
    mod = analyze_rtl(rtl_source, top_module=top_module)
    tb_source = generate_testbench(mod, language, rtl_source=rtl_source)

    base = work_dir or Path(tempfile.mkdtemp(prefix="rtl_verify_"))
    base.mkdir(parents=True, exist_ok=True)
    rtl_path = base / "dut.v"
    tb_path = base / "tb.v"
    rtl_path.write_text(rtl_source, encoding="utf-8")
    tb_path.write_text(tb_source, encoding="utf-8")

    if language == TbLanguage.UVM:
        uvm_note = (
            "UVM testbench generated. Open-source iverilog cannot run UVM; "
            "use Questa/VCS/Xcelium with UVM_HOME. Verilog/SV modes were not simulated."
        )
        return VerificationResult(
            module=mod,
            language=language,
            testbench=tb_source,
            sim_log=uvm_note,
            text_report=_text_summary(mod, tb_source, uvm_note, None, "", "", rtl_source),
            waveform_text="N/A (UVM requires commercial simulator)",
            waveform_html="<p>UVM simulation not run in this tool.</p>",
            success=True,
            work_dir=base,
            vcd_path=None,
            uvm_note=uvm_note,
            status="tb_only",
            simulator="none",
        )

    chosen = None
    if backend and backend.strip():
        chosen = get_backend(backend.strip().lower())
        if chosen is None:
            sim_log = missing_backend_message(backend)
            return _sim_missing_result(mod, language, tb_source, base, sim_log, backend, rtl_source)
        if not chosen.is_available():
            sim_log = missing_backend_message(backend)
            return _sim_missing_result(mod, language, tb_source, base, sim_log, backend, rtl_source)
    else:
        chosen = auto_select(language.value, is_sequential=mod.is_sequential)

    if chosen is None:
        extra = ""
        if mod.is_sequential:
            extra = (
                "\n\nThis design is sequential (clocked). Install Icarus Verilog "
                "to simulate it — the Python reference backend only supports "
                "simple combinational RTL."
            )
        sim_log = missing_backend_message() + extra
        return _sim_missing_result(mod, language, tb_source, base, sim_log, None, rtl_source)

    if chosen.name == "reference" and mod.is_sequential:
        sim_log = (
            "Sequential designs cannot be simulated with the Python reference backend.\n"
            "Install Icarus Verilog: https://bleyer.org/icarus/\n"
            "Add C:\\iverilog\\bin to PATH, then restart the app."
        )
        return _sim_missing_result(mod, language, tb_source, base, sim_log, chosen.name, rtl_source)

    tb_top = f"tb_{mod.name}"
    sim_result = chosen.run(rtl_path, tb_path, base, top=tb_top)
    sim_log = sim_result.log
    vcd_path = sim_result.vcd_path
    backend_used = chosen.name
    backend_version = chosen.version() or ""
    simulator_name = chosen.display_name
    if backend_version:
        simulator_name = f"{simulator_name} ({backend_version})"

    passed = "RESULT: PASS" in sim_log
    status = "pass" if passed else "fail"
    if not passed and mod.is_sequential and chosen.name == "reference":
        status = "sim_missing"
    elif not passed and "not found" in sim_log.lower():
        status = "sim_missing"
    if vcd_path:
        write_dut_info(base, mod)
    wf_text = vcd_to_text(vcd_path) if vcd_path else "No waveform."
    wf_html = vcd_to_html(vcd_path) if vcd_path else "<p>No VCD produced.</p>"

    coverage_report: CoverageReport | None = None
    if vcd_path:
        try:
            module_info = load_module_info(base) or {}
            wave_json = vcd_to_json(vcd_path, module_info=module_info)
            vcd_signals = wave_json.get("signals") if isinstance(wave_json, dict) else None
            if isinstance(vcd_signals, list) and vcd_signals:
                dut_ports = [
                    p.get("name")
                    for p in (module_info.get("ports") or [])
                    if p.get("name")
                ]
                toggle_cov = compute_toggle_coverage(vcd_signals, dut_ports)
                env_by_time = build_env_timeline_from_vcd_signals(vcd_signals)
                interp = CoverageInterpreter(rtl_source, filename="dut.v")
                interp.execute_over_timeline(env_by_time)
                stmt_cov = interp.get_statement_coverage()
                branch_cov = interp.get_branch_coverage()
                fsm_cov = (
                    compute_fsm_coverage(mod, vcd_signals)
                    if mod.is_sequential
                    else FsmCoverage.not_applicable_result(
                        "Design is combinational — no state register detected"
                    )
                )
                overall = compute_overall_percent(
                    stmt_cov,
                    branch_cov,
                    toggle_cov,
                    fsm_cov,
                    is_sequential=mod.is_sequential,
                )
                coverage_report = CoverageReport(
                    statement=stmt_cov,
                    branch=branch_cov,
                    toggle=toggle_cov,
                    fsm=fsm_cov,
                    overall_percent=overall,
                    meta={"dut": mod.name},
                )
                (base / "coverage.json").write_text(
                    json.dumps(coverage_report.to_dict(), indent=2),
                    encoding="utf-8",
                )
        except Exception:
            coverage_report = None
    text_report = _text_summary(
        mod, tb_source, sim_log, wf_text, backend_used, backend_version, rtl_source
    )
    if coverage_report is not None:
        text_report = text_report + "\n\n" + _format_coverage_text(coverage_report, mod)

    return VerificationResult(
        module=mod,
        language=language,
        testbench=tb_source,
        sim_log=sim_log,
        text_report=text_report,
        waveform_text=wf_text,
        waveform_html=wf_html,
        success=passed,
        work_dir=base,
        vcd_path=vcd_path,
        status=status,
        simulator=simulator_name,
        backend_used=backend_used,
        backend_version=backend_version,
        coverage=coverage_report,
    )


def _format_coverage_text(cov: CoverageReport, mod: RtlModule) -> str:
    def pct(x: float) -> str:
        return f"{x:.1f}%"

    stmt = cov.statement
    br = cov.branch
    tg = cov.toggle
    fsm = cov.fsm

    out: list[str] = []
    out.append("=" * 64)
    out.append("CODE COVERAGE")
    out.append("=" * 64)
    out.append(f"  Statement:    {pct(stmt.percent):>6}   ({stmt.hit} / {stmt.total} statements)")
    if stmt.uncovered_lines:
        out.append("                Uncovered lines: " + ", ".join(str(x) for x in stmt.uncovered_lines[:64]))
    out.append("")
    out.append(f"  Branch:       {pct(br.percent):>6}   ({br.hit} / {br.total} branches)")
    missed = [b for b in (br.branches or []) if not b.get('hit')]
    if missed:
        m0 = missed[0]
        out.append(
            f"                Missed: {m0.get('location','')}"
            + f" arm {m0.get('label','')}"
        )
    out.append("")
    out.append(f"  Toggle:       {pct(tg.percent):>6}   ({tg.bits_toggled_both} / {tg.total_bits} bits both directions)")
    partial = []
    for sig, meta in (tg.per_signal or {}).items():
        both = meta.get("bits_both") or []
        if both and not all(bool(x) for x in both):
            partial.append(sig)
    if partial:
        out.append("                Signals with partial toggle: " + ", ".join(partial[:12]))
    out.append("")
    if fsm.not_applicable:
        out.append("  FSM:          N/A     " + (fsm.na_reason or ""))
    else:
        out.append(f"  FSM:          {pct(fsm.state_percent):>6}   ({fsm.states_visited} / {fsm.total_states} states)")
        if fsm.total_transitions:
            out.append(f"                Transitions: {pct(fsm.transition_percent):>6}   ({fsm.transitions_taken} / {fsm.total_transitions})")
    out.append("")
    out.append(f"  OVERALL:      {pct(cov.overall_percent):>6}")
    return "\n".join(out)


def _sim_missing_result(
    mod: RtlModule,
    language: TbLanguage,
    tb_source: str,
    base: Path,
    sim_log: str,
    backend: Optional[str],
    rtl_source: str = "",
) -> VerificationResult:
    return VerificationResult(
        module=mod,
        language=language,
        testbench=tb_source,
        sim_log=sim_log,
        text_report=_text_summary(mod, tb_source, sim_log, None, backend or "", "", rtl_source),
        waveform_text="No waveform — simulator not installed.",
        waveform_html="<p>Install a simulator backend to generate VCD.</p>",
        success=False,
        work_dir=base,
        vcd_path=None,
        status="sim_missing",
        simulator="none",
        backend_used=backend or "",
    )


def _text_summary(
    mod: RtlModule,
    tb: str,
    sim_log: str,
    waveform: str | None,
    backend_used: str,
    backend_version: str,
    rtl_source: str = "",
) -> str:
    seq_line = f"Sequential: {mod.is_sequential}"
    if mod.is_sequential:
        seq_line += f", clock={mod.clock_port}, reset={mod.reset_port}"
        if mod.reset_port:
            seq_line += f" ({'active-low' if mod.reset_active_low else 'active-high'})"
        if mod.state_reg:
            seq_line += f", FSM {mod.state_reg}=[{', '.join(mod.states)}]"
    backend_line = "Backend: "
    if backend_used:
        backend_line += f"{backend_used}"
        if backend_version:
            backend_line += f" ({backend_version})"
    else:
        backend_line += "(none)"
    lines = [
        "=== RTL VERIFICATION REPORT ===",
        f"Module: {mod.name}",
        backend_line,
        seq_line,
        f"Inferred operation: {mod.inferred_op or 'unknown'}",
        f"Self-check: {_self_check_label(mod, rtl_source)}",
        f"Data inputs: {[p.name for p in mod.data_inputs]}",
        f"Outputs: {[p.name for p in mod.outputs]}",
        "",
        "=== GENERATED TESTBENCH ===",
        tb,
        "",
        "=== SIMULATION LOG ===",
        sim_log,
    ]
    if waveform:
        lines.extend(["", "=== WAVEFORM (text) ===", waveform])
    return "\n".join(lines)


def _self_check_label(mod: RtlModule, rtl_source: str) -> str:
    if mod.is_sequential:
        return "monitor-only (sequential)"
    if rtl_source and can_evaluate_combinational(rtl_source, mod):
        return "golden model from RTL assign statements"
    if mod.inferred_op in ("add", "and", "xor", "or", "sub"):
        return f"inferred {mod.inferred_op}"
    return "monitor-only (unsupported RTL shape)"
