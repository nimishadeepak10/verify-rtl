from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import SimulatorBackend
from .icarus import IcarusBackend
from .reference import ReferenceBackend
from .vivado import VivadoBackend
from .symbiyosys import SymbiYosysBackend
from ..rtl_features import needs_systemverilog_simulator

# Registration order (auto_select uses explicit priority below)
ALL_BACKENDS: List[SimulatorBackend] = [
    IcarusBackend(),
    VivadoBackend(),
    ReferenceBackend(),
    SymbiYosysBackend(),
]

def available_backends() -> List[SimulatorBackend]:
    return [b for b in ALL_BACKENDS if b.is_available()]


def formal_backends() -> List[SimulatorBackend]:
    """Formal (property-checking) backends only — never returned by auto_select()."""
    return [b for b in ALL_BACKENDS if b.supports_formal and b.is_available()]


def get_backend(name: str) -> Optional[SimulatorBackend]:
    for b in ALL_BACKENDS:
        if b.name == name:
            return b
    return None


def auto_select(
    language: str = "systemverilog",
    is_sequential: bool = False,
    rtl_source: Optional[str] = None,
) -> Optional[SimulatorBackend]:
    """Pick the best available backend for the language and RTL features."""
    sv_heavy = needs_systemverilog_simulator(rtl_source or "", language)
    priority = (
        ["vivado", "icarus", "reference"]
        if sv_heavy
        else ["icarus", "vivado", "reference"]
    )
    for name in priority:
        b = get_backend(name)
        if b is None or not b.is_available():
            continue
        if b.supports_formal:
            # Defense in depth: a formal backend must never be picked as a
            # simulator fallback, even if a future edit adds its name to
            # `priority` above by mistake. Formal is requested explicitly
            # via formal_backends() / get_backend(), never auto-selected.
            continue
        if language == "uvm" and not b.supports_uvm:
            continue
        if is_sequential and b.name == "reference":
            continue
        return b
    return None

def backend_info_list() -> List[Dict[str, Any]]:
    """JSON-serializable metadata for all registered backends."""
    out: List[Dict[str, Any]] = []
    for b in ALL_BACKENDS:
        avail = b.is_available()
        out.append(
            {
                "name": b.name,
                "display_name": b.display_name,
                "available": avail,
                "version": b.version() if avail else None,
                "supports_uvm": b.supports_uvm,
                "supports_systemverilog": b.supports_systemverilog,
                "supports_formal": b.supports_formal,
            }
        )
    return out


def missing_backend_message(requested: Optional[str] = None) -> str:
    lines = [
        "No simulator backend could run this design.",
        "",
        "Registered backends:",
    ]
    for b in ALL_BACKENDS:
        status = "available" if b.is_available() else "not installed"
        lines.append(f"  - {b.display_name} ({b.name}): {status}")
    if requested:
        lines.extend(["", f"Requested backend '{requested}' is not available."])
    lines.extend(
        [
            "",
            "Install Icarus Verilog: https://bleyer.org/icarus/",
            "Typical Windows path: C:\\iverilog\\bin",
            "",
            "Or install AMD Vivado and select the Vivado XSim backend.",
            "Typical Windows path: C:\\Xilinx\\Vivado\\<version>\\bin",
            "",
            "Testbench was generated successfully; simulation did not run.",
        ]
    )
    return "\n".join(lines)
