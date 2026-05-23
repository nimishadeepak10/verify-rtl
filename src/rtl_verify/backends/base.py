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


class SimulatorBackend(ABC):
    """Abstract base for any simulator that can run a Verilog/SV testbench."""

    name: str = ""
    display_name: str = ""
    supports_uvm: bool = False
    supports_systemverilog: bool = True

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
