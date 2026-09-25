"""Clock-detection fixes, found by testing this project against real,
third-party RTL for the first time (a real async FIFO from
alexforencich/verilog-axis, a genuine dual-clock CDC design).

Two real bugs, confirmed empirically before and after the fix:

1. A single clock with a non-standard name (`clk_i`, not `clk`/`clock`/
   `ck`) was invisible to `_detect_clock_port`'s name-only heuristic --
   `module.clock_port` came back None even though the design is
   genuinely, safely single-clock. Fixed with a structural fallback:
   scan for `posedge <port>` as the first trigger in an
   `always @(posedge X ...)` sensitivity list (deliberately only the
   FIRST trigger, not every `posedge` in the list -- an early version of
   this fix mistook an active-high async reset, `always @(posedge clk or
   posedge rst)`, for a second clock by matching every posedge
   indiscriminately; caught by testing the positive single-clock case,
   not just the negative multi-clock one this was written for).

2. A genuine multi-clock design (module.clock_port is None because there
   really are multiple distinct clocks, e.g. an async FIFO's wr_clk/
   rd_clk) used to silently fall through to generate_formal_wrapper()'s
   combinational `always @(*)` branch, while recommended_formal_config()
   -- looking only at is_sequential, not clock_port -- separately and
   independently picked PDR/"prove" mode meant for genuinely clocked
   designs. Two functions, each seeing only half the picture, produced
   an internally inconsistent formal run. Fixed by having
   generate_formal_wrapper() raise a clear, specific ValueError instead
   of silently building a wrapper that doesn't match reality.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.backends.symbiyosys import SymbiYosysBackend  # noqa: E402
from rtl_verify.formal_props import generate_formal_wrapper, recommended_engine_chain  # noqa: E402

NONSTANDARD_CLOCK_SOURCE = """
module counter_nonstandard_clk (
    input  wire        clk_i,
    input  wire        rst_i,
    output reg  [3:0]  count
);
    always @(posedge clk_i or posedge rst_i) begin
        if (rst_i) count <= 4'd0;
        else       count <= count + 4'd1;
    end
endmodule
"""

MULTI_CLOCK_SOURCE = """
module two_clock_design (
    input  wire wr_clk,
    input  wire rd_clk,
    input  wire d,
    output reg  wr_q,
    output reg  rd_q
);
    always @(posedge wr_clk) wr_q <= d;
    always @(posedge rd_clk) rd_q <= wr_q;
endmodule
"""


def main() -> None:
    # --- 1. Non-standard single clock name: should be detected and prove ---
    print("=== Non-standard single clock name (clk_i) ===")
    mod = analyze_rtl(NONSTANDARD_CLOCK_SOURCE, top_module="counter_nonstandard_clk")
    print(f"clock_port={mod.clock_port!r} has_multiple_clocks={mod.has_multiple_clocks}")
    assert mod.clock_port == "clk_i", f"expected clk_i, got {mod.clock_port!r}"
    assert not mod.has_multiple_clocks

    wrapper_sv = generate_formal_wrapper(mod, [("count_bound", "count <= 4'hF", "assert")])
    chain = recommended_engine_chain(mod, kind="assert")
    backend = SymbiYosysBackend()
    work = Path(tempfile.mkdtemp(prefix="clock_detect_nonstd_"))
    rtl_path = work / "dut.v"
    rtl_path.write_text(NONSTANDARD_CLOCK_SOURCE, encoding="utf-8")
    wp = work / "wrapper.sv"
    wp.write_text(wrapper_sv, encoding="utf-8")
    result = backend.run(
        rtl_path, wp, work, top="counter_nonstandard_clk_formal_top",
        depth=chain[0]["depth"], mode=chain[0]["mode"], engine=chain[0]["engine"], timeout_sec=60,
    )
    print(f"verdict: status={result.status} success={result.success}")
    assert result.status == "PASS" and result.success, "expected a real PROVEN verdict"
    print("OK\n")

    # --- 2. Genuine multi-clock design: should raise, not silently misbuild ---
    print("=== Genuine multi-clock design (wr_clk / rd_clk) ===")
    mod2 = analyze_rtl(MULTI_CLOCK_SOURCE, top_module="two_clock_design")
    print(f"clock_port={mod2.clock_port!r} has_multiple_clocks={mod2.has_multiple_clocks}")
    assert mod2.clock_port is None
    assert mod2.has_multiple_clocks

    try:
        generate_formal_wrapper(mod2, [("trivial", "1'b1", "assert")])
        raise AssertionError("expected generate_formal_wrapper to raise ValueError")
    except ValueError as e:
        print(f"Correctly raised: {e}")
    print("OK\n")

    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
