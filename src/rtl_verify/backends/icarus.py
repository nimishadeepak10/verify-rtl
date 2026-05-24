"""Icarus Verilog (iverilog + vvp) simulator backend."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from .base import BackendResult, SimulatorBackend


def find_iverilog() -> str | None:
    for name in ("iverilog", "iverilog.exe"):
        path = shutil.which(name)
        if path:
            return path
    candidates: list[Path] = []
    for base in (
        Path(r"C:\iverilog"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Icarus Verilog",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Icarus Verilog",
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


class IcarusBackend(SimulatorBackend):
    name = "icarus"
    display_name = "Icarus Verilog"
    supports_uvm = False
    supports_systemverilog = True

    def is_available(self) -> bool:
        return find_iverilog() is not None and find_vvp(find_iverilog()) is not None

    def version(self) -> Optional[str]:
        iverilog = find_iverilog()
        if not iverilog:
            return None
        try:
            r = subprocess.run(
                [iverilog, "-V"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            line = (r.stdout or r.stderr or "").strip().splitlines()[0]
            m = re.search(r"version\s+([\d.]+)", line, re.IGNORECASE)
            return m.group(1) if m else line
        except (subprocess.TimeoutExpired, OSError, IndexError):
            return None

    def run(
        self,
        rtl_path: Path,
        tb_path: Path,
        work_dir: Path,
        top: str = "tb",
    ) -> BackendResult:
        t0 = time.perf_counter()
        iverilog = find_iverilog()
        if not iverilog:
            return BackendResult(
                success=False,
                log="Icarus Verilog (iverilog) not found. Install from https://bleyer.org/icarus/",
                vcd_path=None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
            )

        work_dir.mkdir(parents=True, exist_ok=True)
        work_abs = work_dir.resolve()
        rtl_abs = rtl_path.resolve()
        tb_abs = tb_path.resolve()
        sim_v = work_abs / "sim.vvp"
        vcd = work_abs / "sim.vcd"

        vvp = find_vvp(iverilog)
        if not vvp:
            return BackendResult(
                success=False,
                log="vvp not found (install Icarus Verilog and add bin to PATH).",
                vcd_path=None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
            )

        compile_cmd = [iverilog, "-g2012", "-o", str(sim_v), str(rtl_abs), str(tb_abs)]
        run_cmd = [vvp, str(sim_v)]
        log_lines: list[str] = []

        try:
            c = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
                cwd=str(work_abs),
                timeout=60,
            )
            log_lines.append("=== COMPILE ===")
            log_lines.append(c.stdout or "")
            log_lines.append(c.stderr or "")
            if c.returncode != 0:
                return BackendResult(
                    success=False,
                    log="\n".join(log_lines),
                    vcd_path=None,
                    work_dir=work_abs,
                    duration_sec=time.perf_counter() - t0,
                )

            r = subprocess.run(
                run_cmd,
                capture_output=True,
                text=True,
                cwd=str(work_abs),
                timeout=120,
            )
            log_lines.append("=== SIMULATION ===")
            log_lines.append(r.stdout or "")
            log_lines.append(r.stderr or "")
            ok = r.returncode == 0 and vcd.exists()
            return BackendResult(
                success=ok,
                log="\n".join(log_lines),
                vcd_path=vcd if vcd.exists() else None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
            )
        except subprocess.TimeoutExpired:
            log_lines.append("Simulation timed out.")
            return BackendResult(
                success=False,
                log="\n".join(log_lines),
                vcd_path=None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
            )
        except FileNotFoundError as e:
            log_lines.append(f"Simulator executable missing: {e}")
            return BackendResult(
                success=False,
                log="\n".join(log_lines),
                vcd_path=None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
            )
