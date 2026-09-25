"""Clock-domain-crossing (CDC) and reset-domain-crossing (RDC) checking --
a static structural scan (no solver involved), validated on synthetic
cases with a known-correct answer, then on a real, well-known async FIFO
(alexforencich/verilog-axis) to confirm the heuristic behaves sensibly
against genuinely professional RTL, not just designs built to flatter it.

Fetch the real design once (not committed -- see the URL below) if you
want to re-run the real-design half of this script; the synthetic half
needs nothing external.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.cdc_check import analyze_cdc  # noqa: E402

GOOD_SYNC = """
module good_sync (
    input  wire clk_a,
    input  wire clk_b,
    input  wire d,
    output reg  q_sync
);
    reg d_reg;
    always @(posedge clk_a) d_reg <= d;

    reg stage1, stage2;
    always @(posedge clk_b) begin
        stage1 <= d_reg;
        stage2 <= stage1;
        q_sync <= stage2;
    end
endmodule
"""

BAD_UNSYNC = """
module bad_unsync (
    input  wire clk_a,
    input  wire clk_b,
    input  wire [3:0] data_a,
    output reg  [3:0] sum_b
);
    reg [3:0] data_reg;
    always @(posedge clk_a) data_reg <= data_a;

    always @(posedge clk_b) begin
        sum_b <= data_reg + 4'd1;  // used directly in arithmetic -- no sync
    end
endmodule
"""

COMB_RESET = """
module comb_reset (
    input  wire clk,
    input  wire raw_rst,
    input  wire extra_cond,
    output reg  [3:0] count
);
    wire rst_derived;
    assign rst_derived = raw_rst | extra_cond;

    always @(posedge clk or posedge rst_derived) begin
        if (rst_derived) count <= 4'd0;
        else              count <= count + 4'd1;
    end
endmodule
"""


def main() -> None:
    print("=== Synthetic: proper 2+ stage synchronizer -> expect LIKELY_OK ===")
    mod = analyze_rtl(GOOD_SYNC, top_module="good_sync")
    report = analyze_cdc(mod, GOOD_SYNC)
    assert len(report.crossings) == 1, report.crossings
    c = report.crossings[0]
    print(f"  {c.signal}: depth={c.sync_depth} verdict={c.verdict}")
    assert c.verdict == "LIKELY_OK" and c.sync_depth >= 2, c
    print("OK\n")

    print("=== Synthetic: crossing used directly, no sync -> expect UNSYNCHRONIZED ===")
    mod2 = analyze_rtl(BAD_UNSYNC, top_module="bad_unsync")
    report2 = analyze_cdc(mod2, BAD_UNSYNC)
    assert len(report2.crossings) == 1, report2.crossings
    c2 = report2.crossings[0]
    print(f"  {c2.signal}: depth={c2.sync_depth} verdict={c2.verdict}")
    assert c2.verdict == "UNSYNCHRONIZED" and c2.sync_depth == 0, c2
    print("OK\n")

    print("=== Synthetic: reset derived from combinational logic -> expect RISKY ===")
    mod3 = analyze_rtl(COMB_RESET, top_module="comb_reset")
    report3 = analyze_cdc(mod3, COMB_RESET)
    assert len(report3.reset_signals) == 1, report3.reset_signals
    r = report3.reset_signals[0]
    print(f"  {r.name}: kind={r.kind} verdict={r.verdict}")
    assert r.kind == "combinational" and r.verdict == "RISKY", r
    print("OK\n")

    print("=== Sanity: single-clock design (rv32i_core.v) -> expect zero crossings ===")
    rv32i_source = (ROOT / "examples" / "rv32i_core.v").read_text(encoding="utf-8")
    rv32i_mod = analyze_rtl(rv32i_source, top_module="rv32i_core")
    rv32i_report = analyze_cdc(rv32i_mod, rv32i_source)
    print(f"  domains={list(rv32i_report.domains.keys())} crossings={len(rv32i_report.crossings)}")
    assert len(rv32i_report.crossings) == 0, "single-clock design must never report a crossing"
    print("OK\n")

    external = ROOT.parent / "external_rtl_cache" / "axis_async_fifo.v"
    if external.is_file():
        print("=== Real: axis_async_fifo.v (alexforencich/verilog-axis) ===")
        real_source = external.read_text(encoding="utf-8")
        real_mod = analyze_rtl(real_source, top_module="axis_async_fifo")
        real_report = analyze_cdc(real_mod, real_source)
        print(f"  domains: {list(real_report.domains.keys())}")
        print(f"  crossings: {len(real_report.crossings)}, "
              f"unsynchronized: {len(real_report.unsynchronized_crossings)}")
        for c in real_report.crossings:
            print(f"    {c.signal}: {c.source_domain}->{c.dest_domain} depth={c.sync_depth} verdict={c.verdict}")
        # A well-reviewed, widely-used reference design should not come
        # back with any UNSYNCHRONIZED crossing -- if this ever fails,
        # investigate whether it's a real finding or a heuristic gap
        # before assuming either.
        assert not real_report.unsynchronized_crossings, real_report.unsynchronized_crossings
        print("OK\n")
    else:
        print(
            "(skipping real-design check -- fetch "
            "https://raw.githubusercontent.com/alexforencich/verilog-axis/master/rtl/axis_async_fifo.v "
            f"to {external} to include it)\n"
        )

    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
