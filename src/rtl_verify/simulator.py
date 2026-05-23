# Deprecated: use backends/ instead.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .backends.icarus import IcarusBackend, find_iverilog, find_vvp
from .backends.base import BackendResult


@dataclass
class SimResult:
    success: bool
    log: str
    vcd_path: Path | None
    work_dir: Path


def run_icarus(
    rtl_path: Path,
    tb_path: Path,
    work_dir: Path,
    top: str = "tb",
) -> SimResult:
    """Run Icarus via IcarusBackend (legacy API)."""
    br = IcarusBackend().run(rtl_path, tb_path, work_dir, top=top)
    return SimResult(
        success=br.success,
        log=br.log,
        vcd_path=br.vcd_path,
        work_dir=br.work_dir,
    )


__all__ = ["SimResult", "find_iverilog", "find_vvp", "run_icarus"]
