"""End-to-end: RTL in → testbench → simulate → reports."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .analyzer import RtlModule, analyze_rtl
from .backends.registry import (
    auto_select,
    get_backend,
    missing_backend_message,
)
from .generators.base import TbLanguage, generate_testbench
from .waveform import vcd_to_html, vcd_to_text


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


def run_verification(
    rtl_source: str,
    language: TbLanguage = TbLanguage.SYSTEMVERILOG,
    top_module: Optional[str] = None,
    work_dir: Optional[Path] = None,
    backend: Optional[str] = None,
) -> VerificationResult:
    mod = analyze_rtl(rtl_source, top_module=top_module)
    tb_source = generate_testbench(mod, language)

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
            text_report=_text_summary(mod, tb_source, uvm_note, None, "", ""),
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
            return _sim_missing_result(mod, language, tb_source, base, sim_log, backend)
        if not chosen.is_available():
            sim_log = missing_backend_message(backend)
            return _sim_missing_result(mod, language, tb_source, base, sim_log, backend)
    else:
        chosen = auto_select(language.value)

    if chosen is None:
        sim_log = missing_backend_message()
        return _sim_missing_result(mod, language, tb_source, base, sim_log, None)

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
    wf_text = vcd_to_text(vcd_path) if vcd_path else "No waveform."
    wf_html = vcd_to_html(vcd_path) if vcd_path else "<p>No VCD produced.</p>"
    text_report = _text_summary(
        mod, tb_source, sim_log, wf_text, backend_used, backend_version
    )

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
        status="pass" if passed else "fail",
        simulator=simulator_name,
        backend_used=backend_used,
        backend_version=backend_version,
    )


def _sim_missing_result(
    mod: RtlModule,
    language: TbLanguage,
    tb_source: str,
    base: Path,
    sim_log: str,
    backend: Optional[str],
) -> VerificationResult:
    return VerificationResult(
        module=mod,
        language=language,
        testbench=tb_source,
        sim_log=sim_log,
        text_report=_text_summary(mod, tb_source, sim_log, None, backend or "", ""),
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
        f"Inferred operation: {mod.inferred_op or 'unknown (monitor-only)'}",
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
        lines.extend(["", waveform])
    return "\n".join(lines)
