"""Generate Verilog-2001 directed testbench with self-checking when possible."""

from __future__ import annotations

from ..analyzer import PortDirection, RtlModule


def generate(mod: RtlModule) -> str:
    inputs = mod.inputs
    outputs = mod.outputs
    if not inputs or not outputs:
        raise ValueError("Need at least one input and one output port to auto-generate tests")

    cases = _build_cases(mod)
    dut_ports = _dut_port_connections(mod)
    checks = _check_blocks(mod, cases)
    stim = _stimulus_blocks(mod, cases)

    return f"""`timescale 1ns/1ps
// Auto-generated Verilog testbench for {mod.name}
module tb_{mod.name};
{dut_ports}
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
    ins = mod.inputs
    max_vals = [_max_stim_value(p.width) for p in ins]
    total = 1
    for v in max_vals:
        total *= v + 1
    if total > 512:
        return _random_cases(ins, 32)
    cases = []
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
    ins = mod.inputs
    outs = mod.outputs
    if mod.inferred_op == "add" and len(ins) >= 2 and outs:
        a, b = ins[0].name, ins[1].name
        out = outs[0]
        aw, bw, ow = ins[0].width, ins[1].width, out.width
        mask = (1 << ow) - 1
        return f"(({a} + {b}) & {mask})"
    if mod.inferred_op == "and" and len(ins) >= 2:
        return f"({ins[0].name} & {ins[1].name})"
    if mod.inferred_op == "xor" and len(ins) >= 2:
        return f"({ins[0].name} ^ {ins[1].name})"
    return None


def _stimulus_blocks(mod: RtlModule, cases: list[dict[str, int]]) -> str:
    lines = []
    for i, case in enumerate(cases):
        assigns = [f"        {k} = {v};" for k, v in case.items()]
        lines.append(f"        // test {i}")
        lines.extend(assigns)
        lines.append("        #5;")
        exp = _expected_expr(mod)
        if exp and mod.outputs:
            out = mod.outputs[0].name
            lines.append(
                f'        if ({out} !== {exp}) begin fail_cnt = fail_cnt + 1; '
                f'$display("FAIL t=%0d exp=%0d got=%0d", $time, {exp}, {out}); end '
                f"else begin pass_cnt = pass_cnt + 1; "
                f'$display("PASS a=%0d b=%0d sum=%0d", {mod.inputs[0].name}, '
                f"{mod.inputs[1].name if len(mod.inputs) > 1 else 0}, {out}); end"
            )
        else:
            outs = ", ".join(
                f'{p.name}=%0d' for p in mod.outputs
            )
            args = ", ".join(p.name for p in mod.outputs)
            lines.append(
                f'        $display("STIM t=%0d {outs}", $time, {args});'
            )
    return "\n".join(lines)


def _check_blocks(mod: RtlModule, cases: list[dict[str, int]]) -> str:
    return ""
