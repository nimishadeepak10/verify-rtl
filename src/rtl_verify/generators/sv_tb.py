"""SystemVerilog testbench — wraps waveform-oriented Verilog TB."""

from __future__ import annotations

from ..analyzer import RtlModule
from . import verilog_tb as vtb


def generate(mod: RtlModule, rtl_source: str | None = None, monitor_only: bool = True) -> str:
    base = vtb.generate(mod, rtl_source, monitor_only=monitor_only)
    return base.replace(
        "// Auto-generated testbench for",
        "// Auto-generated SystemVerilog testbench for",
    )
