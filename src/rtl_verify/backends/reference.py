"""Python reference simulator backend (combinational RTL only)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from ..analyzer import analyze_rtl
from ..reference_sim import can_simulate_combinational, run_reference_sim
from .base import BackendResult, SimulatorBackend


class ReferenceBackend(SimulatorBackend):
    name = "reference"
    display_name = "Python reference (combinational only)"
    supports_uvm = False
    supports_systemverilog = False

    def is_available(self) -> bool:
        return True

    def version(self) -> Optional[str]:
        return "built-in"

    def run(
        self,
        rtl_path: Path,
        tb_path: Path,
        work_dir: Path,
        top: str = "tb",
    ) -> BackendResult:
        t0 = time.perf_counter()
        rtl_source = rtl_path.read_text(encoding="utf-8")
        mod = analyze_rtl(rtl_source)

        if not can_simulate_combinational(rtl_source, mod):
            msg = (
                "Reference backend: combinational assign-based RTL only "
                "(sequential designs need Icarus or another Verilog simulator)."
            )
            return BackendResult(
                success=False,
                log=msg,
                vcd_path=None,
                work_dir=work_dir,
                duration_sec=time.perf_counter() - t0,
            )

        ok, log, vcd_path = run_reference_sim(rtl_source, mod, work_dir)
        return BackendResult(
            success=ok,
            log=log,
            vcd_path=vcd_path,
            work_dir=work_dir,
            duration_sec=time.perf_counter() - t0,
        )
