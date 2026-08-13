"""End-to-end: RTL in → testbench → simulate → reports."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .analyzer import RtlModule, analyze_rtl
from .verification_mode import resolve_status
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
from .rtl_features import dut_source_extension, resolve_tb_language
from .synth_check import SynthCheckResult, check_synthesizability
from .sim_errors import extract_errors
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
    verification_mode: str = "waveform"
    verdict: str = "error"
    verification_mode_explanation: str = ""
    synth_synthesizable: bool | None = None
    synth_log: str = ""
    synth_tool: str = ""
    synth_skipped: bool = False
    detected_language: str = ""
    errors: list[str] = field(default_factory=list)


def run_verification(
    rtl_source: str,
    language: TbLanguage = TbLanguage.SYSTEMVERILOG,
    top_module: Optional[str] = None,
    work_dir: Optional[Path] = None,
    backend: Optional[str] = None,
) -> VerificationResult:
    detected = resolve_tb_language(rtl_source, language.value)
    if language != TbLanguage.UVM:
        language = TbLanguage(detected)

    mod = analyze_rtl(rtl_source, top_module=top_module)

    base = work_dir or Path(tempfile.mkdtemp(prefix="rtl_verify_"))
    base.mkdir(parents=True, exist_ok=True)

    synth = check_synthesizability(rtl_source, mod.name, base, language=language)
    (base / "synth_check.log").write_text(synth.log, encoding="utf-8")

    if not synth.skipped and not synth.synthesizable:
        tb_source = ""
        return _synth_failed_result(mod, language, base, synth, rtl_source)

    tb_source = generate_testbench(mod, language, rtl_source=rtl_source, monitor_only=True)
    dut_ext = dut_source_extension(rtl_source, language.value)
    tb_ext = ".sv" if language != TbLanguage.VERILOG else ".v"
    rtl_path = base / f"dut{dut_ext}"
    tb_path = base / f"tb{tb_ext}"
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
        chosen = auto_select(
            language.value,
            is_sequential=mod.is_sequential,
            rtl_source=rtl_source,
        )

    if chosen is None:
        extra = ""
        if mod.is_sequential:
            extra = (
                "\n\nThis design is sequential (clocked). Install Icarus Verilog or "
                "AMD Vivado XSim to simulate it — the Python reference backend only "
                "supports simple combinational RTL."
            )
        sim_log = missing_backend_message() + extra
        return _sim_missing_result(mod, language, tb_source, base, sim_log, None, rtl_source)

    if chosen.name == "reference" and mod.is_sequential:
        sim_log = (
            "Sequential designs cannot be simulated with the Python reference backend.\n"
            "Install Icarus Verilog (https://bleyer.org/icarus/) or AMD Vivado XSim.\n"
            "Add C:\\iverilog\\bin or C:\\Xilinx\\Vivado\\<version>\\bin to PATH, then restart."
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

    from .verification_mode import simulation_compile_ok, simulation_completed

    compile_ok = simulation_compile_ok(sim_log, backend_used)
    sim_completed = simulation_completed(sim_log)
    errors = extract_errors(
        synth.log if synth else "",
        sim_log,
        synth_ok=synth.skipped or synth.synthesizable,
    )
    v_mode, verdict, v_expl, success = resolve_status(
        mod, sim_log, synth, compile_ok=compile_ok, sim_completed=sim_completed
    )
    status = verdict if verdict in ("pass", "fail", "not_synthesizable") else "fail"
    if not sim_completed and "not found" in sim_log.lower():
        status = "sim_missing"
        verdict = "error"
        v_mode = "sim_failed"
    passed = success
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
        mod,
        tb_source,
        sim_log,
        wf_text,
        backend_used,
        backend_version,
        rtl_source,
        verification_mode=v_mode,
        verdict=verdict,
        synth=synth,
        language=language.value,
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
        verification_mode=v_mode,
        verdict=verdict,
        verification_mode_explanation=v_expl,
        synth_synthesizable=synth.synthesizable if not synth.skipped else None,
        synth_log=synth.log,
        synth_tool=synth.tool,
        synth_skipped=synth.skipped,
        detected_language=language.value,
        errors=errors,
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


def _synth_failed_result(
    mod: RtlModule,
    language: TbLanguage,
    base: Path,
    synth: SynthCheckResult,
    rtl_source: str,
) -> VerificationResult:
    expl = (
        "RTL failed Vivado synthesis — not synthesizable. "
        "Fix errors in the synthesis log before running simulation."
    )
    errors = extract_errors(synth.log, "", synth_ok=False)
    report = _text_summary(
        mod,
        "",
        "",
        None,
        "",
        "",
        rtl_source,
        verification_mode="synth_failed",
        verdict="not_synthesizable",
        synth=synth,
        language=language.value,
    )
    return VerificationResult(
        module=mod,
        language=language,
        testbench="",
        sim_log="",
        text_report=report,
        waveform_text="No waveform — synthesis check failed.",
        waveform_html="<p>RTL is not synthesizable. Fix synthesis errors and re-run.</p>",
        success=False,
        work_dir=base,
        vcd_path=None,
        status="not_synthesizable",
        simulator="vivado",
        backend_used="vivado",
        verification_mode="synth_failed",
        verdict="not_synthesizable",
        verification_mode_explanation=expl,
        synth_synthesizable=False,
        synth_log=synth.log,
        synth_tool=synth.tool,
        synth_skipped=False,
        detected_language=language.value,
        errors=errors,
    )


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
    verification_mode: str = "",
    verdict: str = "",
    synth: SynthCheckResult | None = None,
    language: str = "",
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
        f"RTL language: {language or '—'}",
        f"Synthesis: {_synth_summary_line(synth)}",
        backend_line,
        seq_line,
        f"Verification: waveform-based (directed testbench + VCD)",
        f"Mode: {verification_mode or '—'}",
        f"Verdict: {(verdict or '—').upper()}",
        f"Data inputs: {[p.name for p in mod.data_inputs]}",
        f"Outputs: {[p.name for p in mod.outputs]}",
        "",
    ]
    if synth and synth.log:
        title = "=== SYNTHESIS CHECK ===" if not synth.skipped else "=== SYNTHESIS CHECK (skipped) ==="
        lines.extend([title, synth.log, ""])
    if tb:
        lines.extend(["=== GENERATED TESTBENCH ===", tb, ""])
    if sim_log:
        lines.extend(["=== SIMULATION LOG ===", sim_log])
    if waveform:
        lines.extend(["", "=== WAVEFORM (text) ===", waveform])
    return "\n".join(lines)


def _synth_summary_line(synth: SynthCheckResult | None) -> str:
    if synth is None:
        return "—"
    if synth.skipped:
        return "skipped (install Vivado for synthesis validation)"
    return "PASS — synthesizable" if synth.synthesizable else "FAIL — not synthesizable"
