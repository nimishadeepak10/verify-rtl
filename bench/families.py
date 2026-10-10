"""Benchmark instances for evaluating technique selection.

Each instance is a design plus properties whose truth is KNOWN by
construction, so a strategy's answer can be scored as correct, unsound or
unsettled rather than just "returned something". The families are chosen so
that different techniques matter:

  counter_thr   a wide counter compared with a large limit (counter abstraction)
  mac_gate      a wide accumulator gating a small arbiter (cut point / black box)
  fifo_data     a wide payload that never reaches control (data-width reduction)
  ring          a one-hot ring whose property is not inductive (invariants)
  rom           a constant lookup table (ROM to case)
  easy          small designs that need nothing (any selector should add no cost)

`oracle` is the parameter set a human expert would pass; it exists to bound
what any selector could achieve, and is never shown to a strategy.
`truth` maps property name -> whether the property actually holds.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


@dataclass
class Instance:
    id: str
    family: str
    rtl: str
    top: str
    props: List[dict]
    truth: Dict[str, bool]
    oracle: Dict[str, object] = field(default_factory=dict)
    note: str = ""


def _prop(name, expr):
    return {"name": name, "expr": expr, "kind": "assert"}


_TIMEOUT = """
module timeout_ctrl (
    input  wire        clk, input wire rst_n, input wire start,
    output reg  [1:0]  state, output reg timed_out, output reg [31:0] cnt
);
    localparam IDLE = 2'd0, RUN = 2'd1, DONE = 2'd2;
    localparam LIMIT = 32'd__LIMIT__;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin state <= IDLE; timed_out <= 1'b0; cnt <= 32'd0; end
        else case (state)
            IDLE: if (start) begin state <= RUN; cnt <= 32'd0; end
            RUN: begin
                cnt <= cnt + 32'd1;
                if (cnt == LIMIT) begin state <= DONE; timed_out <= 1'b1; end
            end
            DONE: ;
            default: state <= IDLE;
        endcase
    end
endmodule
"""

_FIFO = """
module pfifo #(parameter WIDTH = __W__, parameter DEPTH = 4) (
    input clk, input rst_n, input wr_en, input rd_en,
    input [WIDTH-1:0] wr_data, output [WIDTH-1:0] rd_data,
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


def _ring(n: int) -> str:
    return f"""
module ring{n}(input clk, input rst_n, input adv, output [{n - 1}:0] q);
    reg [{n - 1}:0] st;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) st <= {n}'d1;
        else if (adv) st <= {{st[{n - 2}:0], st[{n - 1}]}};
    assign q = st;
endmodule
"""


def _rom(depth: int) -> str:
    aw = max(1, (depth - 1).bit_length())
    entries = "\n".join(f"        rom[{i}] = 8'd{(i * 7) % 251};" for i in range(depth))
    return f"""
module rom{depth}(input clk, input [{aw - 1}:0] addr, output reg [7:0] data);
    reg [7:0] rom [0:{depth - 1}];
    initial begin
        data = 8'd0;
{entries}
    end
    always @(posedge clk) data <= rom[addr];
endmodule
"""


def counter_thr(limit: int) -> Instance:
    return Instance(
        id=f"counter_thr_{limit}", family="counter_thr",
        rtl=_TIMEOUT.replace("__LIMIT__", str(limit)), top="timeout_ctrl",
        props=[_prop("t_done_when_timed_out", "!timed_out || (state == 2'd2)"),
               _prop("f_never_times_out", "!timed_out")],
        truth={"t_done_when_timed_out": True, "f_never_times_out": False},
        oracle={"counter_abstraction": "cnt"},
        note=f"counter reaches {limit} before timed_out can rise",
    )


def mac_gate(stages: int, mults: int = 3) -> Instance:
    import test_cutpoint_and_decompose as tcd
    return Instance(
        id=f"mac_gate_{stages}x{mults}", family="mac_gate",
        rtl=tcd._wrapper_with_accumulator(stages, mults), top="big_soc_wrapper",
        props=[_prop("t_grant_onehot0", "(grant == 4'd0) || $onehot(grant)"),
               _prop("f_never_grants_2", "grant != 4'b0100")],
        truth={"t_grant_onehot0": True, "f_never_grants_2": False},
        oracle={"cut_signals": "mac_hash"},
    )


def mac_gate_dep(stages: int, mults: int = 3) -> Instance:
    """Same design as mac_gate, but a TRUE property whose truth depends on the
    pipeline's real reset behaviour. Black-boxing the pipeline frees its
    output, so an abstraction-first strategy sees a spurious counterexample
    here and has to pay for it: this is the adversarial case for a proactive
    policy, included so its cost is measured and not just its gain."""
    import test_cutpoint_and_decompose as tcd
    return Instance(
        id=f"mac_gate_dep_{stages}x{mults}", family="mac_gate_dep",
        rtl=tcd._wrapper_with_accumulator(stages, mults), top="big_soc_wrapper",
        props=[_prop("t_result_zero_in_reset", "rst_n || (mac_result == 64'd0)"),
               _prop("f_never_grants_2", "grant != 4'b0100")],
        truth={"t_result_zero_in_reset": True, "f_never_grants_2": False},
        oracle={},
        note="property depends on the pipeline: black-boxing it yields a spurious counterexample",
    )


def mac_gate_dep_only(stages: int, mults: int = 3) -> Instance:
    """The worst case for a proactive policy: EVERY property depends on the
    abstracted block, so the abstraction attempt buys nothing and its whole
    cost is overhead on top of the plain proof."""
    inst = mac_gate_dep(stages, mults)
    inst.id = f"mac_gate_depT_{stages}x{mults}"
    inst.family = "mac_gate_dep_only"
    inst.props = [p for p in inst.props if p["name"].startswith("t_")]
    inst.truth = {k: v for k, v in inst.truth.items() if k.startswith("t_")}
    inst.note = "only the property that needs the real pipeline"
    return inst


def fifo_data(width: int) -> Instance:
    return Instance(
        id=f"fifo_data_{width}", family="fifo_data",
        rtl=_FIFO.replace("__W__", str(width)), top="pfifo",
        props=[_prop("t_not_full_and_empty", "!(full && empty)"), _prop("f_never_full", "!full")],
        truth={"t_not_full_and_empty": True, "f_never_full": False},
        oracle={"data_signals": "wr_data", "data_width_reduction": True},
    )


def ring(n: int) -> Instance:
    half = n // 2
    return Instance(
        id=f"ring_{n}", family="ring", rtl=_ring(n), top=f"ring{n}",
        props=[_prop("t_no_adjacent_pair", f"q != {n}'d{3 << half}"),
               _prop("f_never_bit_half", f"q != {n}'d{1 << half}")],
        truth={"t_no_adjacent_pair": True, "f_never_bit_half": False},
        oracle={"mine_invariants": True},
    )


def rom(depth: int) -> Instance:
    return Instance(
        id=f"rom_{depth}", family="rom", rtl=_rom(depth), top=f"rom{depth}",
        props=[_prop("t_never_255", "data != 8'd255"), _prop("f_never_seven", "data != 8'd7")],
        truth={"t_never_255": True, "f_never_seven": False},
        oracle={"rom_to_case": True},
    )


def easy_fifo() -> Instance:
    src = (ROOT / "examples" / "sync_fifo.v").read_text(encoding="utf-8")
    return Instance(
        id="easy_sync_fifo", family="easy", rtl=src, top="sync_fifo",
        props=[_prop("t_count_le_4", "count <= 3'd4"), _prop("t_full_xor_empty", "!(full && empty)"),
               _prop("f_count_le_3", "count <= 3'd3")],
        truth={"t_count_le_4": True, "t_full_xor_empty": True, "f_count_le_3": False},
        oracle={},
    )


def all_instances(scale: str = "pilot") -> List[Instance]:
    if scale == "pilot":
        return [easy_fifo(), ring(8), counter_thr(3000), fifo_data(64), rom(64), mac_gate(150), mac_gate_dep(150), mac_gate_dep_only(150)]
    return ([easy_fifo()] + [ring(n) for n in (8, 12, 16)] + [counter_thr(n) for n in (50, 3000, 300000)]
            + [fifo_data(w) for w in (32, 128)] + [rom(d) for d in (16, 256)]
            + [mac_gate(s) for s in (100, 300)] + [mac_gate_dep(s) for s in (100, 300)])
