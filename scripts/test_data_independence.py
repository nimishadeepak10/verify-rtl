"""Data independence (Seligman et al., "Formal Verification" Ch.10; FVM
complexity guide): shrink a datapath only when data provably cannot reach
control. Parts 1-2 are static; Part 3 (--solver) needs real engines and
must be run via PowerShell, not Bash.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.data_independence import (  # noqa: E402
    analyze_data_independence, property_data_use, recommend_data_width_reductions,
)

PFIFO = """
module pfifo #(parameter WIDTH = 32, parameter DEPTH = 4) (
    input clk, input rst_n, input wr_en, input rd_en,
    input [WIDTH-1:0] wr_data,
    output [WIDTH-1:0] rd_data,
    output full, output empty
);
    reg [WIDTH-1:0] mem [0:DEPTH-1];
    reg [2:0] wr_ptr, rd_ptr;
    wire [2:0] count = wr_ptr - rd_ptr;
    assign full  = (count == 3'd4);
    assign empty = (count == 3'd0);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin wr_ptr <= 3'd0; rd_ptr <= 3'd0; end
        else begin
            if (wr_en && !full) begin mem[wr_ptr[1:0]] <= wr_data; wr_ptr <= wr_ptr + 3'd1; end
            if (rd_en && !empty) rd_ptr <= rd_ptr + 3'd1;
        end
    end
    assign rd_data = mem[rd_ptr[1:0]];
endmodule
"""

# the data value 8'hFF directly steers control
MAGIC = """
module magic_fifo #(parameter WIDTH = 8) (
    input clk, input rst_n, input wr_en, input [WIDTH-1:0] wr_data, output reg overflow
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) overflow <= 1'b0;
        else if (wr_en && wr_data == 8'hFF) overflow <= 1'b1;
    end
endmodule
"""

# data reaches control only through a stored flag
LAUNDERED = """
module laundered #(parameter WIDTH = 8) (
    input clk, input rst_n, input [WIDTH-1:0] d, output reg [1:0] state
);
    reg flag;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin flag <= 1'b0; state <= 2'd0; end
        else begin
            flag <= d[0];
            if (flag) state <= state + 2'd1;
        end
    end
endmodule
"""

# data flows into a ternary select and into an index
TERNARY = """
module tern #(parameter WIDTH = 8) (input clk, input [WIDTH-1:0] d, input [1:0] a, output reg [3:0] q);
    reg [3:0] t [0:3];
    always @(posedge clk) q <= (d > 8'd5) ? 4'd1 : t[a];
endmodule
"""

INDEXED = """
module idx #(parameter WIDTH = 8) (input clk, input [WIDTH-1:0] d, output reg [3:0] q);
    reg [3:0] t [0:255];
    always @(posedge clk) q <= t[d];
endmodule
"""

# control is data-independent, but the datapath COMPUTES on data
ADDER = """
module adder #(parameter WIDTH = 16) (
    input clk, input rst_n, input go, input [WIDTH-1:0] a, input [WIDTH-1:0] b,
    output reg [WIDTH-1:0] sum, output reg busy
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin sum <= 0; busy <= 1'b0; end
        else begin busy <= go; if (go) sum <= a + b; end
    end
endmodule
"""

SUBMOD = """
module sub(input [7:0] x, output [7:0] y); assign y = x; endmodule
module top #(parameter WIDTH = 8) (input [WIDTH-1:0] d, output [7:0] q);
    sub u (.x(d), .y(q));
endmodule
"""


def _status(src, top, data):
    mod = analyze_rtl(src, top_module=top)
    return analyze_data_independence(mod, src, data), mod


def part1_static() -> None:
    print("=== Part 1: information-flow analysis ===")
    rep, mod = _status(PFIFO, "pfifo", ["wr_data"])
    print(f"  pfifo:      {rep.status}  tainted={rep.tainted}")
    assert rep.status == "INDEPENDENT" and set(rep.tainted) >= {"wr_data", "mem", "rd_data"}, rep
    assert "wr_ptr" not in rep.tainted and "full" not in rep.tainted

    rep, _ = _status(MAGIC, "magic_fifo", ["wr_data"])
    print(f"  magic:      {rep.status}  {[(v.signal, v.sink, v.line) for v in rep.violations]}")
    assert rep.status == "DEPENDENT" and rep.violations[0].sink == "if condition"

    rep, _ = _status(LAUNDERED, "laundered", ["d"])
    print(f"  laundered:  {rep.status}  {[(v.signal, v.sink) for v in rep.violations]}")
    assert rep.status == "DEPENDENT" and rep.violations[0].signal == "flag", "taint must propagate through a reg"

    rep, _ = _status(TERNARY, "tern", ["d"])
    print(f"  ternary:    {rep.status}  {[v.sink for v in rep.violations]}")
    assert rep.status == "DEPENDENT" and any(v.sink == "ternary select" for v in rep.violations)

    rep, _ = _status(INDEXED, "idx", ["d"])
    print(f"  indexed:    {rep.status}  {[v.sink for v in rep.violations]}")
    assert rep.status == "DEPENDENT" and any(v.sink == "array/bit index" for v in rep.violations)

    rep, _ = _status(SUBMOD, "top", ["d"])
    print(f"  submodule:  {rep.status}  unknowns={len(rep.unknowns)}")
    assert rep.status == "UNKNOWN" and rep.unknowns, "must never claim INDEPENDENT through a submodule"

    rep, _ = _status(PFIFO, "pfifo", ["wr_data"])
    assert rep.transport_only and not rep.computations, "a FIFO only moves data"
    rep, _ = _status(ADDER, "adder", ["a", "b"])
    print(f"  adder:      {rep.status}  transport_only={rep.transport_only}  computations={[(c.signal, c.line) for c in rep.computations]}")
    assert rep.status == "INDEPENDENT" and not rep.transport_only and rep.computations[0].signal == "sum"

    rep, _ = _status(PFIFO, "pfifo", ["nonexistent"])
    assert rep.status == "ERROR"
    print("OK\n")


def part2_widths() -> None:
    print("=== Part 2: only parameters that size ONLY data are offered ===")
    mod = analyze_rtl(PFIFO, top_module="pfifo")
    rep, recs = recommend_data_width_reductions(mod, PFIFO, ["wr_data"])
    got = {r.name: r.proposed for r in recs}
    print(f"  pfifo offered: {got}")
    assert got == {"WIDTH": 4}, got        # DEPTH sizes the memory's depth (structure), not data width

    mod = analyze_rtl(MAGIC, top_module="magic_fifo")
    rep, recs = recommend_data_width_reductions(mod, MAGIC, ["wr_data"])
    assert rep.status == "DEPENDENT" and recs == [], "a DEPENDENT design must offer nothing"
    print("  magic offered: nothing (data reaches control)")

    assert property_data_use("!(full && empty)", ["wr_data", "mem"]) == (False, False)
    assert property_data_use("rd_data == wr_data", ["wr_data", "rd_data"]) == (True, False)
    assert property_data_use("rd_data > wr_data", ["wr_data", "rd_data"]) == (True, True)
    assert property_data_use("rd_data + 1 == wr_data", ["wr_data", "rd_data"]) == (True, True)
    print("  property classifier: control-only / equality-only / arithmetic")
    print("OK\n")


def part3_solver() -> None:
    from _formal_call import formal_check

    def call(src, top, props, **kw):
        args = dict(rtl_file=None, rtl_text=src, top_module=top, properties=json.dumps(props),
                    timeout_sec=40, depth_override=0, cross_check=True, blackbox_modules="",
                    auto_blackbox=False, cut_signals="", auto_cutpoint=False, decompose=False,
                    rom_to_case=False, param_overrides="", auto_param_reduction=False,
                    counter_abstraction="", auto_counter_abstraction=False,
                    data_signals="", data_width_reduction=False, auto_data_width_reduction=False)
        args.update(kw)
        return asyncio.run(formal_check(**args))

    print("=== Part 3a: reduced width is EXACT for a control property on a data-independent design ===")
    ctrl = [{"name": "never_full_and_empty", "expr": "!(full && empty)", "kind": "assert"},
            {"name": "full_means_four", "expr": "!full || (wr_ptr - rd_ptr) == 3'd4", "kind": "assert"}]
    ctrl = ctrl[:1]
    plain = call(PFIFO, "pfifo", ctrl, cross_check=False)["properties"][0]["verdict"]
    out = call(PFIFO, "pfifo", ctrl, cross_check=False, data_signals="wr_data", data_width_reduction=True)
    red = out["properties"][0]["verdict"]
    print(f"  WIDTH=32: {plain}   reduced: {red}   applied={out['parameter_reduction']['applied']}")
    assert plain == red == "PROVEN"
    assert out["data_independence"]["status"] == "INDEPENDENT"
    assert out["properties"][0]["cross_check"]["performed"] is False
    print("OK\n")

    print("=== Part 3b: refused when data reaches control, and the refusal is justified ===")
    err = call(MAGIC, "magic_fifo", [{"name": "no_overflow", "expr": "!overflow", "kind": "assert"}],
               data_signals="wr_data", data_width_reduction=True)
    print(f"  {err['error'][:110]}...")
    assert "refused" in err["error"] and err["data_independence"]["status"] == "DEPENDENT"
    assert err["data_independence"]["violations"][0]["sink"] == "if condition"
    prop = [{"name": "no_overflow", "expr": "!overflow", "kind": "assert"}]
    full = call(MAGIC, "magic_fifo", prop, cross_check=False)["properties"][0]["verdict"]
    forced = call(MAGIC, "magic_fifo", prop, cross_check=False, param_overrides="WIDTH=4")["properties"][0]["verdict"]
    print(f"  real WIDTH=8: {full}   WIDTH=4 forced anyway: {forced}   <- the unsound answer the check prevents")
    assert full == "FALSIFIED" and forced == "PROVEN"
    print("OK\n")

    print("=== Part 3c: refusals: arithmetic property, and a data property on a design that computes ===")
    arith = [{"name": "ordered", "expr": "rd_data <= wr_data", "kind": "assert"}]
    err = call(PFIFO, "pfifo", arith, data_signals="wr_data", data_width_reduction=True)
    assert "arithmetic" in err["error"], err
    print(f"  {err['error'][:100]}...")
    err = call(ADDER, "adder", [{"name": "same", "expr": "sum == a", "kind": "assert"}],
               data_signals="a,b", data_width_reduction=True)
    assert "computes on data" in err["error"], err
    print(f"  {err['error'][:100]}...")
    ok = call(ADDER, "adder", [{"name": "busy_follows_go", "expr": "!$past(go) || busy", "kind": "assert"}],
              cross_check=False, data_signals="a,b", data_width_reduction=True)
    print(f"  control-only property on the same adder: {ok['properties'][0]['verdict']} at {ok['parameter_reduction']['applied']}")
    assert ok["properties"][0]["verdict"] == "PROVEN"
    print("OK\n")

    print("=== Part 3d: auto escalation on a REAL timeout refuses to reduce when independence is unknown ===")
    import test_cutpoint_and_decompose as tc
    src = tc._wrapper_with_accumulator()
    out = call(src, "big_soc_wrapper", tc.PROP, cross_check=False, data_signals="mac_a,mac_b",
               auto_data_width_reduction=True)["properties"][0]
    info = out.get("auto_data_width_reduction")
    print(f"  verdict={out['verdict']}  auto_data_width_reduction={json.dumps(info)[:200]}")
    if out["verdict"] in ("TIMEOUT", "UNKNOWN"):
        assert info and info["resolved"] is False and info["independence"]["status"] == "UNKNOWN", info
    else:   # a slow k-induction proved the full design before escalation: nothing was reduced
        assert out["verdict"] == "PROVEN" and not info, out
    print("OK\n")


if __name__ == "__main__":
    part1_static()
    part2_widths()
    if "--solver" in sys.argv:
        part3_solver()
    print("All requested parts passed.")
