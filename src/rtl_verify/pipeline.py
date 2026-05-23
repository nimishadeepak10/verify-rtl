"""End-to-end: RTL in → testbench → simulate → reports."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .analyzer import RtlModule, analyze_rtl
from .generators.base import TbLanguage, generate_testbench
from .reference_sim import can_simulate_combinational, run_reference_sim
from .simulator import find_iverilog, run_icarus
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


def run_verification(
    rtl_source: str,
    language: TbLanguage = TbLanguage.SYSTEMVERILOG,
    top_module: Optional[str] = None,
    work_dir: Optional[Path] = None,
) -> VerificationResult:
    mod = analyze_rtl(rtl_source, top_module=top_module)
    tb_source = generate_testbench(mod, language)

    base = work_dir or Path(tempfile.mkdtemp(prefix="rtl_verify_"))
    base.mkdir(parents=True, exist_ok=True)
    rtl_path = base / "dut.v"
    tb_path = base / "tb.v"
    rtl_path.write_text(rtl_source, encoding="utf-8")
    tb_path.write_text(tb_source, encoding="utf-8")

    uvm_note = ""
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
            text_report=_text_summary(mod, tb_source, uvm_note, None),
            waveform_text="N/A (UVM requires commercial simulator)",
            waveform_html="<p>UVM simulation not run in this tool.</p>",
            success=True,
            work_dir=base,
            vcd_path=None,
            uvm_note=uvm_note,
        )

    sim_log = ""
    vcd_path = None
    simulator_name = "Icarus Verilog" if find_iverilog() else ""

    if find_iverilog():
        sim = run_icarus(rtl_path, tb_path, base)
        sim_log = sim.log
        vcd_path = sim.vcd_path
    elif can_simulate_combinational(rtl_source, mod):
        ok, sim_log, vcd_path = run_reference_sim(rtl_source, mod, base)
        simulator_name = "Python reference (install Icarus for full Verilog sim)"
    else:
        sim_log = (
            "Icarus Verilog (iverilog) not found.\n"
            "Install from https://bleyer.org/icarus/ — typical Windows path: C:\\iverilog\n"
            "Then add C:\\iverilog\\bin to your PATH and restart this app.\n\n"
            "Testbench was generated successfully; simulation could not run."
        )
        return VerificationResult(
            module=mod,
            language=language,
            testbench=tb_source,
            sim_log=sim_log,
            text_report=_text_summary(mod, tb_source, sim_log, None),
            waveform_text="No waveform — simulator not installed.",
            waveform_html="<p>Install Icarus Verilog to generate VCD.</p>",
            success=False,
            work_dir=base,
            vcd_path=None,
            status="sim_missing",
            simulator="none",
        )

    passed = "RESULT: PASS" in sim_log
    wf_text = vcd_to_text(vcd_path) if vcd_path else "No waveform."
    wf_html = vcd_to_html(vcd_path) if vcd_path else "<p>No VCD produced.</p>"
    text_report = _text_summary(mod, tb_source, sim_log, wf_text)

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
    )


def _text_summary(mod: RtlModule, tb: str, sim_log: str, waveform: str | None) -> str:
    lines = [
        "=== RTL VERIFICATION REPORT ===",
        f"Module: {mod.name}",
        f"Inferred operation: {mod.inferred_op or 'unknown (monitor-only)'}",
        f"Inputs: {[p.name for p in mod.inputs]}",
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
