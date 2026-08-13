"""Generate waveform-oriented Verilog/SystemVerilog testbenches (no directed test cases)."""

from __future__ import annotations

import re
from typing import Optional

from ..analyzer import PortDirection, RtlModule


def generate(mod: RtlModule, rtl_source: Optional[str] = None, monitor_only: bool = True) -> str:
    if not monitor_only and not mod.is_sequential:
        from .comb_assert_tb import generate as comb_generate

        return comb_generate(mod, rtl_source)
    del rtl_source
    if not mod.outputs:
        raise ValueError("Need at least one output port to generate a testbench")

    if mod.is_sequential and mod.clock_port:
        return _generate_sequential(mod)
    return _generate_combinational(mod)


def _generate_combinational(mod: RtlModule) -> str:
    stim = _combinational_stimulus(mod)
    return f"""`timescale 1ns/1ps
// Auto-generated testbench for {mod.name} (combinational, waveform verification)
module tb_{mod.name};
{_dut_port_connections(mod)}
    reg ref_clk;
    initial ref_clk = 0;
    always #5 ref_clk = ~ref_clk;

    {mod.name} uut (
{_instance_ports(mod)}
    );

    initial begin
        $dumpfile("sim.vcd");
        $dumpvars(0, tb_{mod.name});
    end

    initial begin
        $display("=== TB: stimulus start (combinational) ===");
{_default_input_init(mod)}
{_unpacked_init_block(mod)}
{stim}
        #20;
        $display("=== SIMULATION COMPLETE ===");
        $display("RESULT: DONE");
        $finish;
    end
endmodule
"""


def _generate_sequential(mod: RtlModule) -> str:
    clk = mod.clock_port
    stim = _sequential_stimulus(mod)
    reset_block = _reset_sequence(mod, clk)
    clock_block = _clock_generator(mod, clk)
    fsm_note = ""
    if mod.state_reg and mod.states:
        fsm_note = f"// FSM: {mod.state_reg} states {{ {', '.join(mod.states)} }}\n"

    return f"""`timescale 1ns/1ps
// Auto-generated testbench for {mod.name} (sequential, waveform verification)
{fsm_note}module tb_{mod.name};
{_dut_port_connections(mod)}

    {mod.name} uut (
{_instance_ports(mod)}
    );

{clock_block}
    initial begin
        $dumpfile("sim.vcd");
        $dumpvars(0, tb_{mod.name});
    end

    initial begin
        $display("=== TB: stimulus start (sequential) ===");
{_default_input_init(mod)}
{_unpacked_init_block(mod)}
{reset_block}
{stim}
        repeat(5) @(posedge {clk});
        $display("=== SIMULATION COMPLETE ===");
        $display("RESULT: DONE");
        $finish;
    end
endmodule
"""


def _default_input_init(mod: RtlModule) -> str:
    lines: list[str] = []
    for p in _stim_inputs(mod):
        lines.append(f"        {p.name} = 0;")
    return "\n".join(lines) + ("\n" if lines else "")


def _combinational_stimulus(mod: RtlModule) -> str:
    ins = _stim_inputs(mod)
    if not ins:
        return "        #100; // no data inputs — observe outputs\n"

    lines = ["        // Apply input patterns — verify behavior in waveforms"]
    steps = min(16, max(8, 2 ** min(4, max(p.width for p in ins))))
    for step in range(steps):
        lines.append(f"        // cycle {step}")
        for i, p in enumerate(ins):
            mod_val = (step * (i + 3) + 1) % max(1, min((1 << min(p.width, 8)) - 1, 255))
            lines.append(f"        {p.name} = {mod_val};")
        lines.append("        #10;")
    return "\n".join(lines)


def _sequential_stimulus(mod: RtlModule) -> str:
    clk = mod.clock_port or "clk"
    ins = _stim_inputs(mod)
    if not ins:
        return f"        repeat(16) @(posedge {clk});\n"

    lines = ["        // Change data inputs across clock cycles"]
    for step in range(12):
        lines.append(f"        // cycle {step}")
        for i, p in enumerate(ins):
            val = (step * (i + 5) + 2) % max(1, min((1 << min(p.width, 8)) - 1, 255))
            lines.append(f"        {p.name} = {val};")
        lines.append(f"        repeat(2) @(posedge {clk});")
    return "\n".join(lines)


def _clock_generator(mod: RtlModule, clk: str) -> str:
    if clk in {p.name for p in mod.ports}:
        return f"""    initial {clk} = 0;
    always #5 {clk} = ~{clk};
"""
    return f"""    reg {clk};
    initial {clk} = 0;
    always #5 {clk} = ~{clk};
"""


def _reset_sequence(mod: RtlModule, clk: str) -> str:
    if not mod.reset_port:
        return f"        repeat(2) @(posedge {clk});\n"
    rst = mod.reset_port
    if mod.reset_active_low:
        return f"""        {rst} = 0;
        repeat(3) @(posedge {clk});
        {rst} = 1;
        repeat(2) @(posedge {clk});
"""
    return f"""        {rst} = 1;
        repeat(3) @(posedge {clk});
        {rst} = 0;
        repeat(2) @(posedge {clk});
"""


def _instance_ports(mod: RtlModule) -> str:
    return ",\n".join(f"        .{p.name}({p.name})" for p in mod.ports)


def _dut_port_connections(mod: RtlModule) -> str:
    decls = []
    for p in mod.ports:
        r = p.range_str()
        if p.direction == PortDirection.INPUT:
            decls.append(f"    reg {r} {p.name};")
        else:
            decls.append(f"    wire {r} {p.name};")
    return "\n".join(decls)


def _stim_inputs(mod: RtlModule) -> list:
    ins = mod.data_inputs if mod.is_sequential else mod.inputs
    return [p for p in ins if not p.is_unpacked_array]


def _unpacked_init_block(mod: RtlModule) -> str:
    lines: list[str] = []
    ins = mod.data_inputs if mod.is_sequential else mod.inputs
    for p in ins:
        if not p.is_unpacked_array:
            continue
        count = 1
        if p.extra_ranges:
            m = re.search(r"\[(\d+)\s*:\s*(\d+)\]", p.extra_ranges)
            if m:
                count = abs(int(m.group(1)) - int(m.group(2))) + 1
        lines.append(f"        begin : init_{p.name}")
        lines.append("            integer _ui;")
        lines.append(f"            for (_ui = 0; _ui < {count}; _ui = _ui + 1)")
        lines.append(f"                {p.name}[_ui] = 0;")
        lines.append("        end")
    return "\n".join(lines)
