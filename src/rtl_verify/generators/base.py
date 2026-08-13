from __future__ import annotations

from enum import Enum

from ..analyzer import RtlModule
from . import sv_tb, uvm_tb, verilog_tb


class TbLanguage(str, Enum):
    VERILOG = "verilog"
    SYSTEMVERILOG = "systemverilog"
    UVM = "uvm"


def generate_testbench(
    mod: RtlModule,
    language: TbLanguage,
    rtl_source: str | None = None,
    monitor_only: bool = True,
) -> str:
    if language == TbLanguage.VERILOG:
        return verilog_tb.generate(mod, rtl_source, monitor_only=monitor_only)
    if language == TbLanguage.UVM:
        return uvm_tb.generate(mod)
    if not monitor_only and not mod.is_sequential:
        from .comb_assert_tb import generate as comb_generate

        return comb_generate(mod, rtl_source)
    return sv_tb.generate(mod, rtl_source, monitor_only=monitor_only)
