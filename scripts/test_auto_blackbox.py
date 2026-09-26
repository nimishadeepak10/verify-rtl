"""Auto black-box/white-box candidate ranking (src/rtl_verify/blackbox.py's
recommend_blackbox_candidates()) -- fast, structural-only tests, no solver
involved. See scripts/test_auto_blackbox_rescue.py for a real, solver-based
end-to-end demonstration of the full escalation path this ranking feeds.

The ranking itself follows criteria researched from real industry sources
(Siemens Verification Horizons' Questa formal team, SemiWiki, lubis-eda,
and the arithmetic-circuit-verification literature) before writing any
code -- see recommend_blackbox_candidates()'s own docstring for the full
citation trail. These tests confirm the ranking logic actually implements
that research correctly on known-shape synthetic designs, and matches
the same real design this project's own earlier black-boxing feature
was validated against.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.blackbox import recommend_blackbox_candidates  # noqa: E402

# A module instantiating three very different real-shaped submodules:
# a large numeric memory array, a wide-arithmetic block, and a small
# plain control block -- confirms the ranking order (memory > arithmetic
# > generic size) matches the researched priority.
MIXED_DESIGN = """
module big_memory_block (
    input  wire        clk,
    input  wire [9:0]  addr,
    input  wire [31:0] wdata,
    input  wire        we,
    output reg  [31:0] rdata
);
    reg [31:0] mem [1023:0];
    always @(posedge clk) begin
        if (we) mem[addr] <= wdata;
        rdata <= mem[addr];
    end
endmodule

module wide_mult_block (
    input  wire        clk,
    input  wire [31:0] a,
    input  wire [31:0] b,
    output reg  [63:0] product
);
    always @(posedge clk) product <= a * b;
endmodule

module small_control_block (
    input  wire clk,
    input  wire req,
    output reg  grant
);
    always @(posedge clk) grant <= req;
endmodule

module mixed_top (
    input  wire        clk,
    input  wire [9:0]  addr,
    input  wire [31:0] wdata,
    input  wire        we,
    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire        req,
    output wire [31:0] rdata,
    output wire [63:0] product,
    output wire        grant
);
    big_memory_block u_mem (.clk(clk), .addr(addr), .wdata(wdata), .we(we), .rdata(rdata));
    wide_mult_block u_mul (.clk(clk), .a(a), .b(b), .product(product));
    small_control_block u_ctrl (.clk(clk), .req(req), .grant(grant));
endmodule
"""

# A small parameterized array (a synchronizer-style register, not a real
# memory) must NOT be flagged as a large-memory candidate -- confirmed
# via a numeric size below the threshold.
SMALL_ARRAY_DESIGN = """
module tiny_sync (
    input  wire clk,
    input  wire in,
    output wire out
);
    reg sync_reg [1:0];
    always @(posedge clk) begin
        sync_reg[0] <= in;
        sync_reg[1] <= sync_reg[0];
    end
    assign out = sync_reg[1];
endmodule

module tiny_top (
    input  wire clk,
    input  wire in,
    output wire out
);
    tiny_sync u_sync (.clk(clk), .in(in), .out(out));
endmodule
"""


def main() -> None:
    print("=== Ranking: memory > arithmetic > generic size, on a mixed design ===")
    mod = analyze_rtl(MIXED_DESIGN, top_module="mixed_top")
    cands = recommend_blackbox_candidates(mod, MIXED_DESIGN)
    print(f"  order: {[(c.module_name, c.reason) for c in cands]}")
    assert len(cands) == 3, cands
    assert [c.module_name for c in cands] == ["big_memory_block", "wide_mult_block", "small_control_block"], cands
    assert cands[0].reason == "large_memory" and "1024" in cands[0].detail, cands[0]
    assert cands[1].reason == "wide_arithmetic", cands[1]
    assert cands[2].reason == "large_module", cands[2]
    # Scores must be strictly ordered, not just the reasons -- a caller
    # relying on candidates[0] as "the" top pick needs this to hold.
    assert cands[0].score > cands[1].score > cands[2].score, cands
    print("OK\n")

    print("=== A small (2-entry) parameterized array must NOT be flagged as large_memory ===")
    tiny_mod = analyze_rtl(SMALL_ARRAY_DESIGN, top_module="tiny_top")
    tiny_cands = recommend_blackbox_candidates(tiny_mod, SMALL_ARRAY_DESIGN)
    print(f"  candidates: {[(c.module_name, c.reason) for c in tiny_cands]}")
    assert len(tiny_cands) == 1, tiny_cands
    assert tiny_cands[0].reason == "large_module", (
        "a 2-entry register array is not a meaningful memory-abstraction candidate", tiny_cands
    )
    print("OK\n")

    print("=== No instantiations -> no candidates (nothing to black-box) ===")
    leaf_source = "module leaf (input wire clk, output reg q); always @(posedge clk) q <= 1'b0; endmodule"
    leaf_mod = analyze_rtl(leaf_source, top_module="leaf")
    leaf_cands = recommend_blackbox_candidates(leaf_mod, leaf_source)
    assert leaf_cands == [], leaf_cands
    print("OK\n")

    print("=== Real: the project's own black-boxing stress design (arbiter4 + "
          "wide_mac_pipeline via big_soc_wrapper) ===")
    # Confirms the ranking picks out the SAME module (wide_mac_pipeline)
    # this project's own scripts/test_blackbox_reduction.py already
    # measured as the real cause of proof-time growth -- the ranking
    # isn't just plausible on paper, it agrees with a result already
    # independently confirmed by real solver runs.
    real_source = "\n".join(
        (ROOT / "examples" / f).read_text(encoding="utf-8")
        for f in ["arbiter4.v", "wide_mac_pipeline.v", "big_soc_wrapper.v"]
    )
    real_mod = analyze_rtl(real_source, top_module="big_soc_wrapper")
    real_cands = recommend_blackbox_candidates(real_mod, real_source)
    print(f"  candidates: {[(c.module_name, c.reason) for c in real_cands]}")
    assert real_cands, "expected at least one candidate"
    assert real_cands[0].module_name == "wide_mac_pipeline", real_cands
    print("OK\n")

    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
