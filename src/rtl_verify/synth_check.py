"""RTL synthesizability check (Vivado synth_design when available)."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .backends.vivado import find_vivado_bin, _tool_cmd
from .generators.base import TbLanguage
from .rtl_features import dut_source_extension, needs_systemverilog_simulator

# Generic 7-series part — only used to validate RTL synthesizability, not for placement.
_DEFAULT_PART = "xc7a35tcpg236-1"


@dataclass
class SynthCheckResult:
    synthesizable: bool
    log: str
    tool: str
    duration_sec: float = 0.0
    skipped: bool = False
    skip_reason: str = ""


def check_synthesizability(
    rtl_source: str,
    module_name: str,
    work_dir: Path,
    language: TbLanguage = TbLanguage.SYSTEMVERILOG,
) -> SynthCheckResult:
    """
    Run a synthesis trial on the DUT.
    Uses Vivado synth_design when installed; otherwise returns skipped with guidance.
    """
    bindir = find_vivado_bin()
    if bindir is None:
        return SynthCheckResult(
            synthesizable=True,
            log=(
                "Synthesis check skipped: Vivado not found.\n"
                "Install AMD Vivado to validate synthesizability before simulation.\n"
                "Simulation will still run if the simulator accepts the RTL."
            ),
            tool="none",
            skipped=True,
            skip_reason="vivado_not_installed",
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    ext = dut_source_extension(rtl_source, language.value)
    dut_path = (work_dir / f"dut{ext}").resolve()
    dut_path.write_text(rtl_source, encoding="utf-8")

    tcl_path = work_dir / "synth_check.tcl"
    log_path = work_dir / "synth_check.log"
    dut_posix = dut_path.as_posix()
    use_sv = ext == ".sv" or needs_systemverilog_simulator(rtl_source, language.value)
    read_cmd = f"read_verilog -sv {dut_posix}" if use_sv else f"read_verilog {dut_posix}"

    tcl = f"""{read_cmd}
synth_design -top {module_name} -part {_DEFAULT_PART} -flatten_hierarchy none
exit
"""
    tcl_path.write_text(tcl, encoding="utf-8")

    vivado = _tool_cmd(bindir, "vivado")
    cmd = vivado + [
        "-mode",
        "batch",
        "-source",
        str(tcl_path.resolve()),
        "-log",
        str(log_path.resolve()),
        "-nolog",
        "-nojournal",
    ]

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(work_dir.resolve()),
            timeout=300,
        )
        parts = []
        if proc.stdout:
            parts.append(proc.stdout.rstrip())
        if proc.stderr:
            parts.append(proc.stderr.rstrip())
        if log_path.is_file():
            parts.append(log_path.read_text(encoding="utf-8", errors="replace").rstrip())
        log = "\n".join(p for p in parts if p)
        ok = _synth_succeeded(log, proc.returncode)
        return SynthCheckResult(
            synthesizable=ok,
            log=log or "(no Vivado output)",
            tool="vivado",
            duration_sec=time.perf_counter() - t0,
        )
    except subprocess.TimeoutExpired:
        return SynthCheckResult(
            synthesizable=False,
            log="Vivado synthesis check timed out after 300s.",
            tool="vivado",
            duration_sec=time.perf_counter() - t0,
        )
    except OSError as exc:
        return SynthCheckResult(
            synthesizable=False,
            log=f"Vivado synthesis check failed to start: {exc}",
            tool="vivado",
            duration_sec=time.perf_counter() - t0,
        )


def _synth_succeeded(log: str, returncode: int) -> bool:
    lower = (log or "").lower()
    if "synth_design completed successfully" in lower:
        return True
    if "synthesis finished with 0 errors" in lower and "critical warnings and 0 errors" in lower:
        return True
    if returncode == 0 and "done synthesizing module" in lower:
        return True
    fail_markers = (
        "error: [synth",
        "error: [common",
        "synthesis failed",
        "out of context",
        "cannot synthesize",
    )
    if any(m in lower for m in fail_markers):
        return False
    return returncode == 0
