"""AMD/Xilinx Vivado XSim simulator backend (xvlog + xelab + xsim)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from .base import BackendResult, SimulatorBackend

_VIVADO_TOOL_NAMES = ("xvlog", "xelab", "xsim")


def _vivado_search_roots() -> list[Path]:
    roots: list[Path] = []
    env_root = os.environ.get("XILINX_VIVADO", "").strip()
    if env_root:
        roots.append(Path(env_root))
    if os.name == "nt":
        roots.append(Path(r"C:\Xilinx\Vivado"))
    else:
        roots.extend(
            [
                Path("/tools/Xilinx/Vivado"),
                Path("/opt/Xilinx/Vivado"),
                Path(os.path.expanduser("~/Xilinx/Vivado")),
            ]
        )
    return roots


def find_vivado_bin() -> Path | None:
    """Return Vivado bin directory containing xvlog, xelab, and xsim."""
    for tool in _VIVADO_TOOL_NAMES:
        found = shutil.which(tool)
        if found:
            bindir = Path(found).resolve().parent
            if all((bindir / t).exists() or shutil.which(t) for t in _VIVADO_TOOL_NAMES):
                return bindir

    suffix = ".bat" if os.name == "nt" else ""
    for root in _vivado_search_roots():
        if not root.is_dir():
            continue
        version_dirs = sorted(root.glob("*"), reverse=True) if root.name == "Vivado" else [root]
        for ver_dir in version_dirs:
            bindir = ver_dir / "bin"
            if not bindir.is_dir():
                continue
            if all((bindir / f"{t}{suffix}").is_file() for t in _VIVADO_TOOL_NAMES):
                return bindir
    return None


def _tool_cmd(bindir: Path, name: str) -> list[str]:
    if os.name == "nt":
        bat = bindir / f"{name}.bat"
        if bat.is_file():
            return [str(bat)]
    exe = bindir / name
    if exe.is_file():
        return [str(exe)]
    found = shutil.which(name)
    if found:
        return [found]
    return [name]


class VivadoBackend(SimulatorBackend):
    name = "vivado"
    display_name = "Vivado XSim"
    supports_uvm = False
    supports_systemverilog = True

    def _bindir(self) -> Path | None:
        return find_vivado_bin()

    def is_available(self) -> bool:
        return self._bindir() is not None

    def version(self) -> Optional[str]:
        bindir = self._bindir()
        if not bindir:
            return None
        try:
            r = subprocess.run(
                _tool_cmd(bindir, "xvlog") + ["--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            for line in (r.stdout or r.stderr or "").splitlines():
                line = line.strip()
                if not line or line.upper() == "ECHO IS OFF.":
                    continue
                m = re.search(r"Vivado Simulator v([\d.]+)", line, re.IGNORECASE)
                if m:
                    return m.group(1)
                m = re.search(r"v([\d.]+)", line)
                if m:
                    return m.group(1)
            return None
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
        bindir = self._bindir()
        work_dir.mkdir(parents=True, exist_ok=True)
        work_abs = work_dir.resolve()

        if not bindir:
            return BackendResult(
                success=False,
                log=(
                    "Vivado XSim not found.\n"
                    "Install Vivado and add its bin directory to PATH, or set XILINX_VIVADO.\n"
                    "Typical Windows path: C:\\Xilinx\\Vivado\\2023.2\\bin"
                ),
                vcd_path=None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
            )

        rtl_abs = rtl_path.resolve()
        tb_abs = tb_path.resolve()
        vcd = work_abs / "sim.vcd"
        use_sv = (
            rtl_abs.suffix.lower() == ".sv"
            or tb_abs.suffix.lower() == ".sv"
            or self._needs_sv_flag(tb_abs)
        )

        xvlog = _tool_cmd(bindir, "xvlog")
        xelab = _tool_cmd(bindir, "xelab")
        xsim = _tool_cmd(bindir, "xsim")

        compile_cmd = xvlog + (["-sv"] if use_sv else []) + [str(rtl_abs), str(tb_abs)]
        _elab_flags = [
            top,
            "-debug",
            "typical",
            "--override_timeunit",
            "--timescale",
            "1ns/1ps",
        ]
        elaborate_cmd = xelab + _elab_flags
        simulate_cmd = xsim + [top, "-R"]

        log_lines: list[str] = []

        def _run_step(label: str, cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            log_lines.append(f"=== {label} ===")
            log_lines.append(" ".join(cmd))
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(work_abs),
                timeout=timeout,
            )
            if result.stdout:
                log_lines.append(result.stdout.rstrip())
            if result.stderr:
                log_lines.append(result.stderr.rstrip())
            return result

        try:
            c = _run_step("XVLOG", compile_cmd, timeout=120)
            if c.returncode != 0:
                return BackendResult(
                    success=False,
                    log="\n".join(log_lines),
                    vcd_path=None,
                    work_dir=work_abs,
                    duration_sec=time.perf_counter() - t0,
                )

            e = _run_step("XELAB", elaborate_cmd, timeout=180)
            if e.returncode != 0:
                return BackendResult(
                    success=False,
                    log="\n".join(log_lines),
                    vcd_path=None,
                    work_dir=work_abs,
                    duration_sec=time.perf_counter() - t0,
                )

            s = _run_step("XSIM", simulate_cmd, timeout=300)
            ok = s.returncode == 0 and vcd.is_file()
            return BackendResult(
                success=ok,
                log="\n".join(log_lines),
                vcd_path=vcd if vcd.is_file() else None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
            )
        except subprocess.TimeoutExpired:
            log_lines.append("Vivado simulation timed out.")
            return BackendResult(
                success=False,
                log="\n".join(log_lines),
                vcd_path=None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
            )
        except FileNotFoundError as exc:
            log_lines.append(f"Vivado executable missing: {exc}")
            return BackendResult(
                success=False,
                log="\n".join(log_lines),
                vcd_path=None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
            )

    @staticmethod
    def _needs_sv_flag(tb_path: Path) -> bool:
        try:
            text = tb_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True
        sv_markers = ("int pass_cnt", "int fail_cnt", "logic ", "bit ", "byte ", "shortint ")
        return any(marker in text for marker in sv_markers)
