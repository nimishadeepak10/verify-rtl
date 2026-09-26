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

# `always @(posedge clk) if (rst) x <= 0; else x <= y;` -- an if/else
# with no top-level begin/end where each arm is itself a bare statement.
# A naive "stop at the first semicolon" scan truncates this at the `if`
# arm and never sees the `else` arm at all -- exactly the real miss found
# in ZipCPU's afifo.v, where the else arm was the actual synchronizer
# capture.
IFELSE_NO_BEGIN = """
module ifelse_no_begin (
    input  wire clk_a,
    input  wire clk_b,
    input  wire rst,
    input  wire d,
    output reg  q
);
    reg d_reg;
    always @(posedge clk_a) d_reg <= d;

    reg stage1;
    always @(posedge clk_b)
        if (rst) stage1 <= 1'b0;
        else     stage1 <= d_reg;

    always @(posedge clk_b)
        if (rst) q <= 1'b0;
        else     q <= stage1;
endmodule
"""

# A concatenation-target shift-register synchronizer -- the idiom real
# ZipCPU-style gray-code pointer synchronizers use, packing several
# cross-domain flop stages into one line: `{a, b} <= {b, source};`.
CONCAT_SHIFT_SYNC = """
module concat_shift_sync (
    input  wire clk_a,
    input  wire clk_b,
    input  wire [3:0] ptr_a,
    output wire [3:0] ptr_b_sync
);
    reg [3:0] ptr_a_reg;
    always @(posedge clk_a) ptr_a_reg <= ptr_a;

    reg [3:0] ptr_b_reg, ptr_cross;
    always @(posedge clk_b)
        { ptr_b_reg, ptr_cross } <= { ptr_cross, ptr_a_reg };

    assign ptr_b_sync = ptr_b_reg;
endmodule
"""

# A design with an `ifdef FORMAL`-only "global clock" abstraction --
# ZipCPU's own `(* gclk *) reg gbl_clk;` proof-only trick, real, common
# convention in SymbiYosys/riscv-formal-style RTL. It must not be mistaken
# for a genuine second clock domain: it doesn't exist in synthesized
# hardware at all.
IFDEF_FORMAL_SCAFFOLD = """
module ifdef_formal_scaffold (
    input  wire clk,
    input  wire d,
    output reg  q
);
    always @(posedge clk) q <= d;

`ifdef FORMAL
    reg gbl_clk;
    reg past_q;
    always @(posedge gbl_clk)
        past_q <= q;
`endif
endmodule
"""


# Two unrelated modules in the same file, each genuinely single-clock on
# its own -- but with DIFFERENT clock names. Scanning the raw file text
# instead of scoping to the target module's own body would misattribute
# the other module's always blocks, fabricating a second clock domain
# and crossings that don't exist in either module. Confirmed a real,
# not hypothetical, risk on picorv32.v (8 modules in one file): the
# actual `picorv32` core's CDC report picked up a completely unrelated
# `picorv32_wb` wrapper module's `wb_clk_i`-clocked logic.
MULTI_MODULE_FILE = """
module core_a (
    input  wire clk_a,
    input  wire d,
    output reg  q
);
    always @(posedge clk_a) q <= d;
endmodule

module core_b (
    input  wire clk_b,
    input  wire d,
    output reg  q
);
    reg stage1, stage2;
    always @(posedge clk_b) begin
        stage1 <= d;
        stage2 <= stage1;
        q <= stage2;
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

    print("=== Synthetic: if/else with no begin/end, capture in the else arm -> expect LIKELY_OK ===")
    mod4 = analyze_rtl(IFELSE_NO_BEGIN, top_module="ifelse_no_begin")
    report4 = analyze_cdc(mod4, IFELSE_NO_BEGIN)
    crossing_sigs = {c.signal for c in report4.crossings}
    print(f"  crossings found: {[(c.signal, c.sync_depth, c.verdict) for c in report4.crossings]}")
    assert "d_reg" in crossing_sigs, "the else-arm capture chain must not be truncated away"
    d_reg_crossing = next(c for c in report4.crossings if c.signal == "d_reg")
    assert d_reg_crossing.verdict == "LIKELY_OK" and d_reg_crossing.sync_depth >= 2, d_reg_crossing
    print("OK\n")

    print("=== Synthetic: concatenation-target shift-register synchronizer -> expect LIKELY_OK depth=2 ===")
    mod5 = analyze_rtl(CONCAT_SHIFT_SYNC, top_module="concat_shift_sync")
    report5 = analyze_cdc(mod5, CONCAT_SHIFT_SYNC)
    assert len(report5.crossings) == 1, report5.crossings
    c5 = report5.crossings[0]
    print(f"  {c5.signal}: depth={c5.sync_depth} verdict={c5.verdict}")
    assert c5.signal == "ptr_a_reg" and c5.verdict == "LIKELY_OK" and c5.sync_depth == 2, c5
    print("OK\n")

    print("=== Synthetic: `ifdef FORMAL`-only clock abstraction -> must not appear as a real domain ===")
    mod6 = analyze_rtl(IFDEF_FORMAL_SCAFFOLD, top_module="ifdef_formal_scaffold")
    report6 = analyze_cdc(mod6, IFDEF_FORMAL_SCAFFOLD)
    print(f"  domains={list(report6.domains.keys())} crossings={len(report6.crossings)}")
    assert list(report6.domains.keys()) == ["clk"], "gbl_clk is formal-only scaffolding, not a real clock domain"
    assert len(report6.crossings) == 0
    print("OK\n")

    print("=== Synthetic: multi-module file, scan must scope to the target module only ===")
    mod_a = analyze_rtl(MULTI_MODULE_FILE, top_module="core_a")
    report_a = analyze_cdc(mod_a, MULTI_MODULE_FILE)
    mod_b = analyze_rtl(MULTI_MODULE_FILE, top_module="core_b")
    report_b = analyze_cdc(mod_b, MULTI_MODULE_FILE)
    print(f"  core_a domains={list(report_a.domains.keys())} crossings={len(report_a.crossings)}")
    print(f"  core_b domains={list(report_b.domains.keys())} crossings={len(report_b.crossings)}")
    assert list(report_a.domains.keys()) == ["clk_a"], report_a.domains
    assert list(report_b.domains.keys()) == ["clk_b"], report_b.domains
    assert len(report_a.crossings) == 0 and len(report_b.crossings) == 0
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

    picorv32_path = ROOT.parent / "external_rtl_cache" / "picorv32.v"
    if picorv32_path.is_file():
        print("=== Real: picorv32.v (YosysHQ/picorv32) -- multi-module file, 3000+ lines ===")
        pico_source = picorv32_path.read_text(encoding="utf-8")
        # picorv32 itself (the core) is genuinely single-clock. Its own
        # file also defines picorv32_wb, a *separate* module with its own
        # wb_clk_i clock -- the scan must not let that leak into the
        # core's report (a real bug found here: an earlier version scanned
        # the raw file text instead of scoping to the target module's own
        # body, misattributing picorv32_wb's clock domain to picorv32).
        pico_mod = analyze_rtl(pico_source, top_module="picorv32")
        pico_report = analyze_cdc(pico_mod, pico_source)
        print(f"  picorv32 domains={list(pico_report.domains.keys())} crossings={len(pico_report.crossings)}")
        assert list(pico_report.domains.keys()) == ["clk"], pico_report.domains
        assert len(pico_report.crossings) == 0, pico_report.crossings

        wb_mod = analyze_rtl(pico_source, top_module="picorv32_wb")
        wb_report = analyze_cdc(wb_mod, pico_source)
        print(f"  picorv32_wb domains={list(wb_report.domains.keys())}")
        assert list(wb_report.domains.keys()) == ["wb_clk_i"], wb_report.domains
        print("OK\n")
    else:
        print(
            "(skipping picorv32 check -- fetch "
            "https://raw.githubusercontent.com/YosysHQ/picorv32/master/picorv32.v "
            f"to {picorv32_path} to include it)\n"
        )

    eth_path = ROOT.parent / "external_rtl_cache" / "eth_mac_1g_fifo_combined.v"
    if eth_path.is_file():
        print("=== Real: eth_mac_1g_fifo + its real dependencies, concatenated "
              "(alexforencich/verilog-ethernet + verilog-axis) -- 3500+ lines, 7 modules ===")
        # eth_mac_1g_fifo.v alone is a thin ~330-line wrapper around
        # submodules defined in separate files (eth_mac_1g, an async FIFO
        # adapter, the async FIFO itself, GMII rx/tx, an LFSR for CRC) --
        # this project doesn't resolve cross-file dependencies on its own
        # (a documented limitation), so all 6 real files are concatenated
        # here, exercising that documented workaround on a genuinely
        # larger, hierarchical, real multi-clock SoC assembly than
        # picorv32.v (bigger file, more modules, a different codebase).
        eth_source = eth_path.read_text(encoding="utf-8")

        top_mod = analyze_rtl(eth_source, top_module="eth_mac_1g_fifo")
        top_report = analyze_cdc(top_mod, eth_source)
        print(f"  eth_mac_1g_fifo domains={list(top_report.domains.keys())} "
              f"crossings={len(top_report.crossings)}")
        # A real 3-clock-domain design (tx_clk/rx_clk/logic_clk) -- more
        # domains than anything else validated so far.
        assert set(top_report.domains.keys()) == {"tx_clk", "rx_clk", "logic_clk"}, top_report.domains
        assert not top_report.unsynchronized_crossings, top_report.unsynchronized_crossings

        # The real dual-clock FIFO buried 4 modules deep in this combined
        # file must report the exact same 12-crossing result already
        # validated standalone above -- confirms module-scoping holds at
        # this larger scale, not just picorv32's.
        afifo_mod = analyze_rtl(eth_source, top_module="axis_async_fifo")
        afifo_report = analyze_cdc(afifo_mod, eth_source)
        print(f"  axis_async_fifo (embedded) crossings={len(afifo_report.crossings)} "
              f"(expect 12, matching the standalone result above)")
        assert len(afifo_report.crossings) == 12, afifo_report.crossings
        assert not afifo_report.unsynchronized_crossings

        # A purely combinational adapter module in the same file must not
        # pick up any neighboring module's clock domain.
        adapter_mod = analyze_rtl(eth_source, top_module="axis_async_fifo_adapter")
        adapter_report = analyze_cdc(adapter_mod, eth_source)
        assert adapter_report.domains == {}, adapter_report.domains
        print("OK\n")
    else:
        print(
            "(skipping eth_mac_1g_fifo check -- fetch eth_mac_1g_fifo.v, eth_mac_1g.v, "
            "axis_async_fifo_adapter.v, axis_async_fifo.v, axis_gmii_rx.v, axis_gmii_tx.v, "
            "lfsr.v from alexforencich/verilog-ethernet and verilog-axis, concatenate them, "
            f"and save the result to {eth_path} to include it)\n"
        )

    eth10g_path = ROOT.parent / "external_rtl_cache" / "eth_mac_10g_fifo_combined.v"
    if eth10g_path.is_file():
        print("=== Real: eth_mac_phy_10g_fifo + its real dependencies, concatenated "
              "(alexforencich/verilog-ethernet + verilog-axis) -- 5000+ lines, 14 modules ===")
        # A real 10G MAC/PHY design: deeper hierarchy than the 1G case
        # above (fifo -> phy_10g -> rx_if -> frame_sync/ber_mon/watchdog,
        # 4 levels vs. the 1G design's 3) and roughly 40% more modules.
        # Same documented multi-file workaround: all 11 new dependency
        # files concatenated with the 3 already fetched for the 1G case
        # (axis_async_fifo_adapter, axis_async_fifo, lfsr are shared).
        eth10g_source = eth10g_path.read_text(encoding="utf-8")

        top10g_mod = analyze_rtl(eth10g_source, top_module="eth_mac_phy_10g_fifo")
        top10g_report = analyze_cdc(top10g_mod, eth10g_source)
        print(f"  eth_mac_phy_10g_fifo domains={list(top10g_report.domains.keys())} "
              f"crossings={len(top10g_report.crossings)}")
        assert set(top10g_report.domains.keys()) == {"tx_clk", "rx_clk", "logic_clk"}, top10g_report.domains
        assert not top10g_report.unsynchronized_crossings, top10g_report.unsynchronized_crossings

        # axis_async_fifo now sits 4 modules deep (fifo -> ... -> adapter
        # -> fifo) in this even larger file -- must still match the
        # standalone 12-crossing result exactly.
        afifo10g_mod = analyze_rtl(eth10g_source, top_module="axis_async_fifo")
        afifo10g_report = analyze_cdc(afifo10g_mod, eth10g_source)
        print(f"  axis_async_fifo (embedded, 4 levels deep) crossings={len(afifo10g_report.crossings)} "
              f"(expect 12)")
        assert len(afifo10g_report.crossings) == 12, afifo10g_report.crossings

        # A pure-wrapper module (instantiation only, no own always blocks)
        # must correctly report zero domains, not inherit a neighbor's.
        wrapper_mod = analyze_rtl(eth10g_source, top_module="eth_mac_phy_10g")
        wrapper_report = analyze_cdc(wrapper_mod, eth10g_source)
        assert wrapper_report.domains == {}, wrapper_report.domains
        print("OK\n")
    else:
        print(
            "(skipping eth_mac_phy_10g_fifo check -- fetch eth_mac_phy_10g_fifo.v, "
            "eth_mac_phy_10g.v, eth_mac_phy_10g_rx.v, eth_mac_phy_10g_tx.v, "
            "eth_phy_10g_rx_if.v, eth_phy_10g_tx_if.v, axis_baser_rx_64.v, axis_baser_tx_64.v, "
            "eth_phy_10g_rx_frame_sync.v, eth_phy_10g_rx_ber_mon.v, eth_phy_10g_rx_watchdog.v "
            "from alexforencich/verilog-ethernet, plus axis_async_fifo_adapter.v, "
            "axis_async_fifo.v, and lfsr.v (already used above), concatenate them all, "
            f"and save the result to {eth10g_path} to include it)\n"
        )

    picosoc_path = ROOT.parent / "external_rtl_cache" / "picosoc_icebreaker_combined.v"
    if picosoc_path.is_file():
        print("=== Real: PicoSoC on iCEBreaker (YosysHQ/picorv32) -- a real, deployed "
              "FPGA board SoC, 4000+ lines, 16 modules ===")
        # The full board-level design as actually shipped: icebreaker.v
        # (top, board I/O + reset generation) + ice40up5k_spram.v (vendor
        # SPRAM wrapper) + picosoc.v (bus/address decode, defines its own
        # picosoc_regs/picosoc_mem submodules) + spimemio.v (+ its own
        # spimemio_xfer submodule) + simpleuart.v + picorv32.v (the core
        # already validated standalone, now embedded in a 16-module file
        # spanning two different picorv32-repo subdirectories concatenated
        # together). No PLL and no dual-clock memory anywhere in this
        # design -- it's genuinely single-clock by construction, so every
        # module reporting zero crossings is the correct, honest answer,
        # not a checker limitation.
        picosoc_source = picosoc_path.read_text(encoding="utf-8")

        for top in ["icebreaker", "picosoc", "spimemio", "simpleuart"]:
            m = analyze_rtl(picosoc_source, top_module=top)
            r = analyze_cdc(m, picosoc_source)
            print(f"  {top}: domains={list(r.domains.keys())} crossings={len(r.crossings)}")
            assert len(r.crossings) == 0, (top, r.crossings)

        # picorv32 and picorv32_wb, now embedded in this even larger file
        # (16 modules, spanning two different subdirectories of the same
        # repo concatenated together), must still match their standalone
        # register counts and domains exactly.
        pico_mod = analyze_rtl(picosoc_source, top_module="picorv32")
        pico_report = analyze_cdc(pico_mod, picosoc_source)
        print(f"  picorv32 (embedded): domains={ {k: len(v.registers) for k, v in pico_report.domains.items()} }")
        assert list(pico_report.domains.keys()) == ["clk"]
        assert len(pico_report.domains["clk"].registers) == 177, pico_report.domains["clk"].registers

        wb_mod = analyze_rtl(picosoc_source, top_module="picorv32_wb")
        wb_report = analyze_cdc(wb_mod, picosoc_source)
        assert list(wb_report.domains.keys()) == ["wb_clk_i"]
        assert len(wb_report.domains["wb_clk_i"].registers) == 9
        print("OK\n")
    else:
        print(
            "(skipping PicoSoC check -- fetch icebreaker.v, ice40up5k_spram.v, picosoc.v, "
            "spimemio.v, simpleuart.v from YosysHQ/picorv32's picosoc/ directory, plus "
            "picorv32.v (already used above) from the repo root, concatenate icebreaker.v "
            "first (it must precede picosoc.v) through picorv32.v last, and save the result "
            f"to {picosoc_path} to include it)\n"
        )

    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
