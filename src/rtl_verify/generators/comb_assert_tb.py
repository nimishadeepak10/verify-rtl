"""Self-checking combinational testbench generator with protocol assertions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from ..analyzer import Port, PortDirection, RtlModule
from ..rtl_profile import RtlProfile

PortMap = Union[RtlModule, RtlProfile, Mapping[str, Any]]


@dataclass(frozen=True)
class _P:
    """Normalized port view for TB generation."""

    name: str
    direction: str
    width: int
    msb: int
    lsb: int
    range_str: str
    is_scalar: bool
    is_packed_array: bool
    outer_len: int
    inner_width: int

    @property
    def is_matrix(self) -> bool:
        return self.is_packed_array and self.outer_len > 1 and self.inner_width > 1

    def decl_reg(self) -> str:
        return f"    reg {self.range_str} {self.name};"

    def decl_wire(self) -> str:
        return f"    wire {self.range_str} {self.name};"

    def all_zeros(self) -> str:
        if self.is_matrix:
            return f"tb_fill_{self.name}_const({self.inner_width}'b0);"
        if self.width <= 1:
            return f"{self.name} = 1'b0;"
        return f"{self.name} = {self.width}'b0;"

    def all_ones(self) -> str:
        if self.is_matrix:
            mask = (1 << self.inner_width) - 1
            return f"tb_fill_{self.name}_const({self.inner_width}'h{mask:x});"
        if self.width <= 1:
            return f"{self.name} = 1'b1;"
        return f"{self.name} = {{{self.width}{{1'b1}}}};"

    def max_val(self) -> str:
        return self.all_ones()

    def min_val(self) -> str:
        return self.all_zeros()

    def alternating(self) -> str:
        if self.is_matrix:
            return f"tb_fill_{self.name}_alt;"
        pat = _alternating_literal(self.width)
        return f"{self.name} = {pat};"

    def random_assign(self, mask: bool = True) -> str:
        if self.is_matrix:
            return f"tb_random_{self.name}();"
        if mask and self.width > 1:
            return f"{self.name} = $urandom() & {{{self.width}{{1'b1}}}};"
        return f"{self.name} = $urandom();"


def generate(port_map: PortMap, rtl_source: Optional[str] = None) -> str:
    """Generate a self-checking combinational testbench string."""
    del rtl_source
    ctx = _build_context(port_map)
    if not ctx.outputs:
        raise ValueError("Need at least one output port to generate a testbench")
    if ctx.is_sequential:
        raise ValueError("comb_assert_tb only supports combinational designs")

    parts = [
        _header(ctx),
        _declarations(ctx),
        _dut_instance(ctx),
        _packed_helpers(ctx),
        _io_tasks(ctx),
        _check_task(ctx),
        _main_initial(ctx),
        "endmodule\n",
    ]
    return "\n".join(parts)


def generate_from_dict(profile: Mapping[str, Any]) -> str:
    """Convenience entry when the analyzer returns a plain dict / RtlProfile.to_dict()."""
    return generate(profile)


def _build_context(port_map: PortMap) -> "_Ctx":
    if isinstance(port_map, RtlModule):
        mod_name = port_map.name
        is_seq = port_map.is_sequential
        ports = [_port_from_analyzer(p) for p in port_map.ports]
    elif isinstance(port_map, RtlProfile):
        mod_name = port_map.module_name
        is_seq = port_map.is_sequential
        ports = [_port_from_info(p) for p in port_map.ports]
    else:
        mod_name = str(port_map.get("module_name") or port_map.get("name") or "dut")
        is_seq = bool(port_map.get("is_sequential", False))
        raw_ports = port_map.get("ports") or []
        ports = [_port_from_info(p) for p in raw_ports]

    inputs = [p for p in ports if p.direction == "input"]
    outputs = [p for p in ports if p.direction == "output"]

    nl = lambda s: s.lower()
    req = _first(inputs, lambda p: re.search(r"(^|_)req(uest)?($|_)", nl(p.name)))
    grant = _first(outputs, lambda p: nl(p.name) == "grant")
    grant_valid = _first(outputs, lambda p: "grant_valid" in nl(p.name) or nl(p.name) == "valid")
    grant_idx = _first(outputs, lambda p: "grant_idx" in nl(p.name) or nl(p.name) == "grant_id")
    weight_in = _first(inputs, lambda p: "weight" in nl(p.name))
    weight_out = _first(outputs, lambda p: nl(p.name).endswith("_weight") or nl(p.name) == "grant_weight")

    sweep = [
        p
        for p in inputs
        if re.search(r"weight|priority|\bsel\b|select|mode", nl(p.name))
        and p.name != (weight_in.name if weight_in else "")
    ]
    if weight_in and weight_in not in sweep:
        sweep.append(weight_in)

    ctrl = [
        p
        for p in inputs
        if re.search(r"req|valid|\ben\b|enable", nl(p.name))
        and p not in sweep
    ]
    pair = [p for p in inputs if re.search(r"last_grant|prev|mask", nl(p.name))]

    bus_inputs = [
        p
        for p in inputs
        if p.width > 1 and p not in sweep and p not in ctrl and p not in pair
    ]

    return _Ctx(
        mod_name=mod_name,
        is_sequential=is_seq,
        inputs=inputs,
        outputs=outputs,
        req=req,
        grant=grant,
        grant_valid=grant_valid,
        grant_idx=grant_idx,
        weight_in=weight_in,
        weight_out=weight_out,
        sweep_ports=sweep,
        ctrl_ports=ctrl,
        pair_ports=pair,
        bus_inputs=bus_inputs,
    )


@dataclass
class _Ctx:
    mod_name: str
    is_sequential: bool
    inputs: List[_P]
    outputs: List[_P]
    req: Optional[_P]
    grant: Optional[_P]
    grant_valid: Optional[_P]
    grant_idx: Optional[_P]
    weight_in: Optional[_P]
    weight_out: Optional[_P]
    sweep_ports: List[_P]
    ctrl_ports: List[_P]
    pair_ports: List[_P]
    bus_inputs: List[_P]


def _port_from_analyzer(p: Port) -> _P:
    outer = 1
    if p.extra_ranges:
        m = re.search(r"\[(\d+)\s*:\s*(\d+)\]", p.extra_ranges)
        if m:
            outer = abs(int(m.group(1)) - int(m.group(2))) + 1
    inner = abs(p.msb - p.lsb) + 1
    return _P(
        name=p.name,
        direction=p.direction.value,
        width=p.width,
        msb=p.msb,
        lsb=p.lsb,
        range_str=p.range_str(),
        is_scalar=p.is_scalar,
        is_packed_array=bool(p.extra_ranges) or not p.is_scalar,
        outer_len=outer,
        inner_width=inner,
    )


def _port_from_info(p: Any) -> _P:
    if isinstance(p, _P):
        return p
    if isinstance(p, Port):
        return _port_from_analyzer(p)
    if hasattr(p, "name"):
        d = {
            "name": p.name,
            "direction": getattr(p, "direction", "input"),
            "width": getattr(p, "width", 1),
            "msb": getattr(p, "msb", 0),
            "lsb": getattr(p, "lsb", 0),
            "range_str": getattr(p, "range_str", ""),
            "is_scalar": getattr(p, "is_scalar", True),
            "is_packed_array": getattr(p, "is_packed_array", False),
            "packed_dims": getattr(p, "packed_dims", []),
        }
    else:
        d = dict(p)
    range_str = d.get("range_str") or ""
    if callable(range_str):
        range_str = range_str()
    packed = d.get("packed_dims") or []
    outer = 1
    inner = abs(int(d.get("msb", 0)) - int(d.get("lsb", 0))) + 1
    if len(packed) >= 2:
        m0 = re.search(r"\[(\d+)\s*:\s*(\d+)\]", packed[0])
        m1 = re.search(r"\[(\d+)\s*:\s*(\d+)\]", packed[1])
        if m0:
            outer = abs(int(m0.group(1)) - int(m0.group(2))) + 1
        if m1:
            inner = abs(int(m1.group(1)) - int(m1.group(2))) + 1
    elif len(packed) == 1:
        m0 = re.search(r"\[(\d+)\s*:\s*(\d+)\]", packed[0])
        if m0:
            outer = abs(int(m0.group(1)) - int(m0.group(2))) + 1
    direction = d.get("direction", "input")
    if hasattr(direction, "value"):
        direction = direction.value
    return _P(
        name=d["name"],
        direction=str(direction),
        width=int(d.get("width", 1)),
        msb=int(d.get("msb", 0)),
        lsb=int(d.get("lsb", 0)),
        range_str=range_str,
        is_scalar=bool(d.get("is_scalar", d.get("width", 1) == 1)),
        is_packed_array=bool(d.get("is_packed_array", len(packed) > 1)),
        outer_len=outer,
        inner_width=inner,
    )


def _first(items: Sequence[_P], pred) -> Optional[_P]:
    for item in items:
        if pred(item):
            return item
    return None


def _alternating_literal(width: int) -> str:
    val = 0
    for i in range(width):
        if i % 2 == 0:
            val |= 1 << i
    if width <= 1:
        return "1'b1"
    return f"{width}'b{val:b}"


def _header(ctx: _Ctx) -> str:
    return f"""`timescale 1ns/1ps
// Auto-generated self-checking testbench for {ctx.mod_name} (combinational)
module tb_{ctx.mod_name};"""


def _declarations(ctx: _Ctx) -> str:
    lines: List[str] = []
    for p in ctx.inputs:
        lines.append(p.decl_reg())
    for p in ctx.outputs:
        lines.append(p.decl_wire())
    lines.append("    int test_num;")
    return "\n".join(lines)


def _dut_instance(ctx: _Ctx) -> str:
    conns = ",\n".join(f"        .{p.name}({p.name})" for p in ctx.inputs + ctx.outputs)
    return f"""
    {ctx.mod_name} uut (
{conns}
    );"""


def _packed_helpers(ctx: _Ctx) -> str:
    lines: List[str] = []
    matrix_ports = [p for p in ctx.inputs if p.is_matrix]
    for p in matrix_ports:
        ow, iw = p.outer_len, p.inner_width
        lines.extend(
            [
                f"    task automatic tb_fill_{p.name}_const(input logic [{iw - 1}:0] val);",
                "        int i, j;",
                "        begin",
                f"            for (i = 0; i < {ow}; i = i + 1)",
                f"                for (j = 0; j < {iw}; j = j + 1)",
                f"                    {p.name}[i][j] = val;",
                "        end",
                "    endtask",
                "",
                f"    task automatic tb_fill_{p.name}_row_const(input int row, input logic [{iw - 1}:0] val);",
                "        int j;",
                "        begin",
                f"            for (j = 0; j < {iw}; j = j + 1)",
                f"                {p.name}[row][j] = val;",
                "        end",
                "    endtask",
                "",
                f"    task automatic tb_fill_{p.name}_alt;",
                "        int i, j;",
                "        begin",
                f"            for (i = 0; i < {ow}; i = i + 1)",
                f"                for (j = 0; j < {iw}; j = j + 1)",
                f"                    {p.name}[i][j] = (i ^ j) & 1;",
                "        end",
                "    endtask",
                "",
                f"    task automatic tb_random_{p.name};",
                "        int i;",
                "        begin",
                f"            for (i = 0; i < {ow}; i = i + 1)",
                f"                {p.name}[i] = $urandom() & {{{iw}{{1'b1}}}};",
                "        end",
                "    endtask",
                "",
            ]
        )
    return "\n".join(lines)


def _io_tasks(ctx: _Ctx) -> str:
    display_args = []
    for p in ctx.inputs + ctx.outputs:
        if p.width <= 8 and not p.is_matrix:
            display_args.append(f"{p.name}=%b")
        else:
            display_args.append(f"{p.name}=%0h")
    fmt_in = ", ".join(display_args) if display_args else "no ports"
    args = ", ".join(p.name for p in ctx.inputs + ctx.outputs)

    init_lines = ["    task automatic tb_init_inputs;", "        begin"]
    for p in ctx.inputs:
        if p.is_matrix:
            init_lines.append(f"            tb_fill_{p.name}_const({p.inner_width}'b0);")
        else:
            init_lines.append(f"            {p.all_zeros()}")
    init_lines.extend(["        end", "    endtask", ""])

    return "\n".join(init_lines) + f"""
    task automatic tb_display_io(input string tag);
        begin
            $display("[%s] test=%0d {fmt_in}", tag, test_num, {args});
        end
    endtask
"""


def _check_task(ctx: _Ctx) -> str:
    lines = [
        "    task automatic tb_check_outputs(input string tag);",
        "        int i;",
        "        int ones;",
        "        int found_idx;",
        "        begin",
    ]

    if ctx.req and ctx.grant_valid:
        lines.extend(
            [
                f"            if (({ctx.req.name} != '0) && !{ctx.grant_valid.name})",
                f"                $fatal(1, \"[%s] {ctx.grant_valid.name}=0 but {ctx.req.name}=%0h\", tag, {ctx.req.name});",
                f"            if (({ctx.req.name} == '0) && {ctx.grant_valid.name})",
                f"                $fatal(1, \"[%s] {ctx.grant_valid.name}=1 but {ctx.req.name}=0\", tag);",
            ]
        )

    if ctx.grant and ctx.grant_valid:
        gw = ctx.grant.outer_len if ctx.grant.is_matrix else ctx.grant.width
        lines.append(f"            if ({ctx.grant_valid.name}) begin")
        lines.extend(
            [
                f"                ones = $countones({ctx.grant.name});",
                f"                if (ones != 1)",
                f"                    $fatal(1, \"[%s] {ctx.grant.name} not one-hot: ones=%0d val=%0h\", tag, ones, {ctx.grant.name});",
            ]
        )
        if ctx.grant_idx:
            lines.extend(
                [
                    "                found_idx = -1;",
                    f"                for (i = 0; i < {gw}; i = i + 1)",
                    f"                    if ({ctx.grant.name}[i]) found_idx = i;",
                    f"                if (found_idx < 0 || $unsigned({ctx.grant_idx.name}) != found_idx)",
                    f"                    $fatal(1, \"[%s] {ctx.grant_idx.name}=%0d does not match {ctx.grant.name}=%0h\", tag, {ctx.grant_idx.name}, {ctx.grant.name});",
                ]
            )
        if ctx.weight_in and ctx.weight_out:
            if ctx.weight_in.is_matrix:
                wref = f"{ctx.weight_in.name}[found_idx]"
            else:
                wref = ctx.weight_in.name
            lines.extend(
                [
                    f"                if ({ctx.weight_out.name} !== {wref})",
                    f"                    $fatal(1, \"[%s] {ctx.weight_out.name}=%0d expected %0d from {ctx.weight_in.name}\", tag, {ctx.weight_out.name}, {wref});",
                ]
            )
        lines.append("            end")

    lines.extend(["        end", "    endtask", ""])
    return "\n".join(lines)


def _stim_block(ctx: _Ctx, tag: str, assign_lines: List[str]) -> str:
    body = "\n".join(f"            {ln}" for ln in assign_lines)
    return f"""        begin : {tag}
            test_num = test_num + 1;
            $display("=== TEST: {tag} ===");
{body}
            #1;
            tb_display_io("{tag}");
            tb_check_outputs("{tag}");
        end
"""


def _main_initial(ctx: _Ctx) -> str:
    tests: List[str] = [
        "    initial begin",
        "        $dumpfile(\"sim.vcd\");",
        f"        $dumpvars(0, tb_{ctx.mod_name});",
        "        test_num = 0;",
        "        tb_init_inputs();",
        "",
    ]

    # 1. All zeros
    tests.append(_stim_block(ctx, "all_zeros", [p.all_zeros() for p in ctx.inputs]))

    # 2. All ones
    tests.append(_stim_block(ctx, "all_ones", [p.all_ones() for p in ctx.inputs]))

    # 3. One-hot sweep on bus / req inputs
    one_hot_targets = list(ctx.bus_inputs)
    if ctx.req and ctx.req not in one_hot_targets:
        one_hot_targets.insert(0, ctx.req)
    for p in one_hot_targets:
        w = p.outer_len if p.is_matrix else p.width
        if w <= 1:
            continue
        lines = [
            f"for (int _oh = 0; _oh < {w}; _oh = _oh + 1) begin",
            "    tb_init_inputs();",
        ]
        if p.is_matrix:
            lines.append(f"    {p.name}[_oh] = {{{p.inner_width}{{1'b1}}}};")
        else:
            lines.append(f"    {p.name} = ({w}'d1 << _oh);")
        lines.extend(
            [
                "    #1;",
                f'    tb_display_io("one_hot_{p.name}");',
                f'    tb_check_outputs("one_hot_{p.name}");',
                "end",
            ]
        )
        tests.append(
            f"""        begin : one_hot_{p.name}
            test_num = test_num + 1;
            $display("=== TEST: one_hot_{p.name} ===");
            {chr(10).join("            " + ln for ln in lines)}
        end
"""
        )

    # 4. Corner cases per input
    corner_assigns: List[str] = []
    for p in ctx.inputs:
        corner_assigns.append(f"// corner min {p.name}")
        corner_assigns.append(p.min_val())
    tests.append(_stim_block(ctx, "corner_min", corner_assigns))

    corner_max: List[str] = []
    for p in ctx.inputs:
        corner_max.append(p.max_val())
    tests.append(_stim_block(ctx, "corner_max", corner_max))

    corner_alt: List[str] = []
    for p in ctx.inputs:
        corner_alt.append(p.alternating())
    tests.append(_stim_block(ctx, "corner_alternate", corner_alt))

    # 5. Sweeps for weight / priority / sel / mode
    for p in ctx.sweep_ports:
        sweep_max = min((1 << min(p.inner_width if p.is_matrix else p.width, 8)) - 1, 15)
        if p.is_matrix:
            block = [
                f"for (int _sv = 0; _sv <= {sweep_max}; _sv = _sv + 1) begin",
                "    tb_init_inputs();",
                f"    tb_fill_{p.name}_const(_sv);",
                "    #1;",
                f'    tb_display_io("sweep_{p.name}");',
                f'    tb_check_outputs("sweep_{p.name}");',
                "end",
            ]
        else:
            w = p.width
            block = [
                f"for (int _sv = 0; _sv <= {sweep_max}; _sv = _sv + 1) begin",
                "    tb_init_inputs();",
                f"    {p.name} = _sv[{w - 1}:0];",
                "    #1;",
                f'    tb_display_io("sweep_{p.name}");',
                f'    tb_check_outputs("sweep_{p.name}");',
                "end",
            ]
        tests.append(
            f"""        begin : sweep_{p.name}
            test_num = test_num + 1;
            $display("=== TEST: sweep_{p.name} ===");
            {chr(10).join("            " + ln for ln in block)}
        end
"""
        )

    # 6. req / valid / en exhaustive or random
    for p in ctx.ctrl_ports:
        w = p.width
        if w <= 8:
            block = [
                f"for (int _cv = 0; _cv < (1 << {w}); _cv = _cv + 1) begin",
                "    tb_init_inputs();",
                f"    {p.name} = _cv[{w - 1}:0];",
                "    #1;",
                f'    tb_display_io("exhaust_{p.name}");',
                f'    tb_check_outputs("exhaust_{p.name}");',
                "end",
            ]
            label = f"exhaust_{p.name}"
        else:
            block = [
                "repeat (32) begin",
                "    tb_init_inputs();",
                f"    {p.random_assign()}",
                "    #1;",
                f'    tb_display_io("random_{p.name}");',
                f'    tb_check_outputs("random_{p.name}");',
                "end",
            ]
            label = f"random_{p.name}"
        tests.append(
            f"""        begin : {label}
            test_num = test_num + 1;
            $display("=== TEST: {label} ===");
            {chr(10).join("            " + ln for ln in block)}
        end
"""
        )

    # 7. Pair last_grant / prev / mask with req sweeps
    if ctx.pair_ports and ctx.req:
        pair = ctx.pair_ports[0]
        pw = pair.width
        rw = ctx.req.width
        if rw <= 8:
            req_loop = f"for (int _rv = 0; _rv < (1 << {rw}); _rv = _rv + 1)"
            req_assign = f"{ctx.req.name} = _rv[{rw - 1}:0];"
        else:
            req_loop = "repeat (16)"
            req_assign = f"{ctx.req.random_assign()}"
        block = [
            f"for (int _pv = 0; _pv < (1 << {pw}); _pv = _pv + 1) begin",
            f"    {pair.name} = _pv[{pw - 1}:0];",
            f"    {req_loop} begin",
            "        tb_init_inputs();",
            f"        {pair.name} = _pv[{pw - 1}:0];",
            f"        {req_assign}",
            "        #1;",
            f'        tb_display_io("paired_{pair.name}_{ctx.req.name}");',
            f'        tb_check_outputs("paired_{pair.name}_{ctx.req.name}");',
            "    end",
            "end",
        ]
        tests.append(
            f"""        begin : paired_{pair.name}_{ctx.req.name}
            test_num = test_num + 1;
            $display("=== TEST: paired_{pair.name}_{ctx.req.name} ===");
            {chr(10).join("            " + ln for ln in block)}
        end
"""
        )

    tests.extend(
        [
            "",
            '        $display("=== ALL TESTS PASSED (%0d categories) ===", test_num);',
            '        $display("RESULT: PASS");',
            "        $finish;",
            "    end",
        ]
    )
    return "\n".join(tests)
