from __future__ import annotations

from enum import Enum

from ..analyzer import RtlModule
from . import sv_tb, uvm_tb, verilog_tb


class TbLanguage(str, Enum):
    VERILOG = "verilog"
    SYSTEMVERILOG = "systemverilog"
    UVM = "uvm"


def generate_testbench(mod: RtlModule, language: TbLanguage) -> str:
    if language == TbLanguage.VERILOG:
        return verilog_tb.generate(mod)
    if language == TbLanguage.UVM:
        return uvm_tb.generate(mod)
    return sv_tb.generate(mod)
