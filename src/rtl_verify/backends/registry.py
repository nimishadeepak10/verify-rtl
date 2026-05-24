from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import SimulatorBackend
from .icarus import IcarusBackend
from .reference import ReferenceBackend

# Order matters: first available wins for auto-select
ALL_BACKENDS: List[SimulatorBackend] = [
    IcarusBackend(),
    ReferenceBackend(),
]


def available_backends() -> List[SimulatorBackend]:
    return [b for b in ALL_BACKENDS if b.is_available()]


def get_backend(name: str) -> Optional[SimulatorBackend]:
    for b in ALL_BACKENDS:
        if b.name == name:
            return b
    return None


def auto_select(
    language: str = "systemverilog",
    is_sequential: bool = False,
) -> Optional[SimulatorBackend]:
    """Pick the best available backend for the language."""
    for b in available_backends():
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
            "Testbench was generated successfully; simulation did not run.",
        ]
    )
    return "\n".join(lines)
