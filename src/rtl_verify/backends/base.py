from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class BackendResult:
    success: bool
    log: str
    vcd_path: Optional[Path]
    work_dir: Path
    duration_sec: float = 0.0
    # Raw status word from a formal backend (e.g. SymbiYosys: PASS/FAIL/
    # ERROR/TIMEOUT/UNKNOWN), when the backend distinguishes one. None for
    # backends that don't (simulators, or when the status file is missing).
    # Lets a caller tell "the property was falsified" (FAIL — a legitimate
    # proof result) apart from "the tool itself failed" (ERROR/TIMEOUT —
    # worth retrying), which `success` alone collapses into the same False.
    status: Optional[str] = None


class SimulatorBackend(ABC):
    """Abstract base for any simulator that can run a Verilog/SV testbench."""

    name: str = ""
    display_name: str = ""
    supports_uvm: bool = False
    supports_systemverilog: bool = True
    # True for formal (property-checking) backends, as opposed to simulators.
    # auto_select() must never treat a formal backend as a simulator
    # fallback — formal is a different question, requested explicitly.
    supports_formal: bool = False

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this simulator is installed and runnable."""

    @abstractmethod
    def version(self) -> Optional[str]:
        """Return version string if available, else None."""

    @abstractmethod
    def run(
        self,
        rtl_path: Path,
        tb_path: Path,
        work_dir: Path,
        top: str = "tb",
    ) -> BackendResult:
        """Compile + simulate. Should produce sim.vcd in work_dir on success."""
