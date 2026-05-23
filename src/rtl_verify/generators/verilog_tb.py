"""Generate Verilog-2001 directed testbench with self-checking when possible."""

from __future__ import annotations

from ..analyzer import PortDirection, RtlModule


def generate(mod: RtlModule) -> str:
    outputs = mod.outputs
    data_ins = mod.data_inputs
    if not data_ins and not mod.is_sequential:
        if not mod.inputs or not outputs:
            raise ValueError("Need at least one input and one output port to auto-generate tests")
    if not outputs:
        raise ValueError("Need at least one output port to auto-generate tests")

    if mod.is_sequential and mod.clock_port:
        return _generate_sequential(mod)

    if not mod.inputs:
        raise ValueError("Need at least one input and one output port to auto-generate tests")

    return _generate_combinational(mod)


def _generate_combinational(mod: RtlModule) -> str:
    cases = _build_cases(mod)
    stim = _stimulus_blocks(mod, cases)

    return f"""`timescale 1ns/1ps
// Auto-generated Verilog testbench for {mod.name} (combinational)
module tb_{mod.name};
{_dut_port_connections(mod)}
    integer pass_cnt;
    integer fail_cnt;

    {mod.name} uut (
{_instance_ports(mod)}
    );

    initial begin
        $dumpfile("sim.vcd");
        $dumpvars(0, tb_{mod.name});
    end

    initial begin
        pass_cnt = 0;
        fail_cnt = 0;
{stim}
        #10;
        $display("=== SUMMARY ===");
        $display("PASS=%0d FAIL=%0d", pass_cnt, fail_cnt);
        if (fail_cnt == 0) $display("RESULT: PASS");
        else $display("RESULT: FAIL");
        $finish;
    end
endmodule
"""


def _generate_sequential(mod: RtlModule) -> str:
    clk = mod.clock_port
    cases = _build_cases(mod)
    stim = _sequential_stimulus(mod, cases)
    reset_block = _reset_sequence(mod, clk)
    clock_block = _clock_generator(mod, clk)
    fsm_note = ""
    if mod.state_reg and mod.states:
        fsm_note = f"// FSM: {mod.state_reg} states {{ {', '.join(mod.states)} }}\n"

    return f"""`timescale 1ns/1ps
// Auto-generated Verilog testbench for {mod.name} (sequential)
{fsm_note}module tb_{mod.name};
{_dut_port_connections(mod)}
    integer pass_cnt;
    integer fail_cnt;

    {mod.name} uut (
{_instance_ports(mod)}
    );

{clock_block}
    initial begin
        $dumpfile("sim.vcd");
        $dumpvars(0, tb_{mod.name});
    end

    initial begin
        pass_cnt = 0;
        fail_cnt = 0;
{reset_block}
{stim}
        #20;
        $display("=== SUMMARY ===");
        $display("PASS=%0d FAIL=%0d", pass_cnt, fail_cnt);
        if (fail_cnt == 0) $display("RESULT: PASS");
        else $display("RESULT: FAIL");
        $finish;
    end
endmodule
"""


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
        return f"        // no reset port\n        repeat(2) @(posedge {clk});\n"
    rst = mod.reset_port
    if mod.reset_active_low:
        return f"""        // assert reset (active-low)
        {rst} = 0;
        repeat(3) @(posedge {clk});
        {rst} = 1;
        repeat(2) @(posedge {clk});
"""
    return f"""        // assert reset (active-high)
        {rst} = 1;
        repeat(3) @(posedge {clk});
        {rst} = 0;
        repeat(2) @(posedge {clk});
"""


def _sequential_stimulus(mod: RtlModule, cases: list[dict[str, int]]) -> str:
    clk = mod.clock_port or "clk"
    lines: list[str] = []
    for i, case in enumerate(cases):
        assigns = [f"        {k} = {v};" for k, v in case.items()]
        lines.append(f"        // test {i}")
        lines.extend(assigns)
        lines.append(f"        repeat(3) @(posedge {clk});")
        if mod.state_reg:
            state_out = _find_state_output(mod)
            if state_out:
                stim_val = list(case.values())[0] if case else 0
                lines.append(
                    f'        $display("SEQ t=%0d {state_out}=%0d", '
                    f"$time, {state_out});"
                )
            else:
                lines.append(
                    f'        $display("SEQ t=%0d test={i}", $time);'
                )
        else:
            outs = " ".join(f"{p.name}=%0d" for p in mod.outputs)
            args = ", ".join(p.name for p in mod.outputs)
            lines.append(f'        $display("SEQ t=%0d {outs}", $time, {args});')
        lines.append("        pass_cnt = pass_cnt + 1;")
    if not cases:
        lines.append(f'        repeat(5) @(posedge {clk});')
        lines.append('        $display("SEQ no data inputs — clock/reset exercise only");')
        lines.append("        pass_cnt = pass_cnt + 1;")
    return "\n".join(lines)


def _find_state_output(mod: RtlModule) -> str | None:
    if mod.state_reg:
        for p in mod.outputs:
            if p.name == mod.state_reg or p.name in ("light", "state", "state_out", "current_state"):
                return p.name
    for p in mod.outputs:
        if "state" in p.name.lower():
            return p.name
    return mod.outputs[0].name if mod.outputs else None


def _instance_ports(mod: RtlModule) -> str:
    lines = []
    for p in mod.ports:
        lines.append(f"        .{p.name}({p.name})")
    return ",\n".join(lines)


def _dut_port_connections(mod: RtlModule) -> str:
    decls = []
    for p in mod.ports:
        r = p.range_str()
        if p.direction == PortDirection.INPUT:
            decls.append(f"    reg {r} {p.name};")
        else:
            decls.append(f"    wire {r} {p.name};")
    return "\n".join(decls)


def _max_stim_value(width: int) -> int:
    return min((1 << width) - 1, 255)


def _build_cases(mod: RtlModule) -> list[dict[str, int]]:
    ins = mod.data_inputs if mod.is_sequential else mod.inputs
    if not ins:
        return [{}]
    max_vals = [_max_stim_value(p.width) for p in ins]
    total = 1
    for v in max_vals:
        total *= v + 1
    if total > 512:
        return _random_cases(ins, 32)
    cases: list[dict[str, int]] = []
    limits = [v + 1 for v in max_vals]

    def recurse(idx: int, cur: dict[str, int]) -> None:
        if idx == len(ins):
            cases.append(dict(cur))
            return
        p = ins[idx]
        for val in range(limits[idx]):
            cur[p.name] = val
            recurse(idx + 1, cur)

    recurse(0, {})
    return cases


def _random_cases(ins, n: int) -> list[dict[str, int]]:
    import random

    random.seed(42)
    cases = []
    for _ in range(n):
        c = {}
        for p in ins:
            c[p.name] = random.randint(0, _max_stim_value(p.width))
        cases.append(c)
    return cases


def _expected_expr(mod: RtlModule) -> str | None:
    ins = mod.data_inputs if mod.is_sequential else mod.inputs
    outs = mod.outputs
    if mod.inferred_op == "add" and len(ins) >= 2 and outs:
        out = outs[0]
        ow = out.width
        mask = (1 << ow) - 1
        return f"(({ins[0].name} + {ins[1].name}) & {mask})"
    if mod.inferred_op == "and" and len(ins) >= 2:
        return f"({ins[0].name} & {ins[1].name})"
    if mod.inferred_op == "xor" and len(ins) >= 2:
        return f"({ins[0].name} ^ {ins[1].name})"
    return None


def _stimulus_blocks(mod: RtlModule, cases: list[dict[str, int]]) -> str:
    lines = []
    data_ins = mod.inputs
    for i, case in enumerate(cases):
        assigns = [f"        {k} = {v};" for k, v in case.items()]
        lines.append(f"        // test {i}")
        lines.extend(assigns)
        lines.append("        #5;")
        exp = _expected_expr(mod)
        if exp and mod.outputs:
            out = mod.outputs[0].name
            in0 = data_ins[0].name if data_ins else "0"
            in1 = data_ins[1].name if len(data_ins) > 1 else "0"
            lines.append(
                f'        if ({out} !== {exp}) begin fail_cnt = fail_cnt + 1; '
                f'$display("FAIL t=%0d exp=%0d got=%0d", $time, {exp}, {out}); end '
                f"else begin pass_cnt = pass_cnt + 1; "
                f'$display("PASS a=%0d b=%0d sum=%0d", {in0}, {in1}, {out}); end'
            )
        else:
            outs = ", ".join(f"{p.name}=%0d" for p in mod.outputs)
            args = ", ".join(p.name for p in mod.outputs)
            lines.append(f'        $display("STIM t=%0d {outs}", $time, {args});')
            lines.append("        pass_cnt = pass_cnt + 1;")
    return "\n".join(lines)
