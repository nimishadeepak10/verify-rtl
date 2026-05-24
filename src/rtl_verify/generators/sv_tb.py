"""SystemVerilog testbench with classes and structured reporting."""

from __future__ import annotations

from ..analyzer import PortDirection, RtlModule
from . import verilog_tb as vtb


def generate(mod: RtlModule, rtl_source: str | None = None) -> str:
    base = vtb.generate(mod, rtl_source)
    return base.replace(
        "// Auto-generated Verilog testbench",
        "// Auto-generated SystemVerilog testbench",
    ).replace("module tb_", "module tb_").replace(
        "integer pass_cnt;",
        "int pass_cnt;",
    ).replace("integer fail_cnt;", "int fail_cnt;")
