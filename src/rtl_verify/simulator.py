"""Run Icarus Verilog simulation and capture logs."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SimResult:
    success: bool
    log: str
    vcd_path: Path | None
    work_dir: Path


def find_iverilog() -> str | None:
    for name in ("iverilog", "iverilog.exe"):
        path = shutil.which(name)
        if path:
            return path
    import os
    from pathlib import Path as P

    candidates: list[P] = []
    for base in (
        P(r"C:\iverilog"),
        P(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Icarus Verilog",
        P(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Icarus Verilog",
    ):
        if base.is_dir():
            candidates.extend([base / "bin" / "iverilog.exe", base / "iverilog.exe"])
            for sub in base.glob("**/iverilog.exe"):
                candidates.append(sub)
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def find_vvp(iverilog_path: str | None = None) -> str | None:
    for name in ("vvp", "vvp.exe"):
        path = shutil.which(name)
        if path:
            return path
    if iverilog_path:
        vvp = Path(iverilog_path).parent / "vvp.exe"
        if vvp.is_file():
            return str(vvp)
    return None


def run_icarus(
    rtl_path: Path,
    tb_path: Path,
    work_dir: Path,
    top: str = "tb",
) -> SimResult:
    """Compile and run with iverilog + vvp. top is testbench module name prefix."""
    iverilog = find_iverilog()
    if not iverilog:
        return SimResult(
            success=False,
            log="Icarus Verilog (iverilog) not found. Install from https://bleyer.org/icarus/",
            vcd_path=None,
            work_dir=work_dir,
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    sim_v = work_dir / "sim.vvp"
    vcd = work_dir / "sim.vcd"

    compile_cmd = [
        iverilog,
        "-g2012",
        "-o",
        str(sim_v),
        str(rtl_path),
        str(tb_path),
    ]
    vvp = find_vvp(iverilog)
    if not vvp:
        return SimResult(
            success=False,
            log="vvp not found (install Icarus Verilog and add bin to PATH).",
            vcd_path=None,
            work_dir=work_dir,
        )
    run_cmd = [vvp, str(sim_v)]

    log_lines: list[str] = []

    try:
        c = subprocess.run(
            compile_cmd,
            capture_output=True,
            text=True,
            cwd=str(work_dir),
            timeout=60,
        )
        log_lines.append("=== COMPILE ===")
        log_lines.append(c.stdout or "")
        log_lines.append(c.stderr or "")
        if c.returncode != 0:
            return SimResult(False, "\n".join(log_lines), None, work_dir)

        r = subprocess.run(
            run_cmd,
            capture_output=True,
            text=True,
            cwd=str(work_dir),
            timeout=120,
        )
        log_lines.append("=== SIMULATION ===")
        log_lines.append(r.stdout or "")
        log_lines.append(r.stderr or "")
        ok = r.returncode == 0 and vcd.exists()
        return SimResult(ok, "\n".join(log_lines), vcd if vcd.exists() else None, work_dir)
    except subprocess.TimeoutExpired:
        log_lines.append("Simulation timed out.")
        return SimResult(False, "\n".join(log_lines), None, work_dir)
    except FileNotFoundError as e:
        log_lines.append(f"Simulator executable missing: {e}")
        return SimResult(False, "\n".join(log_lines), None, work_dir)
