"""analyze_rtl()'s `ifdef`/`ifndef` resolution -- a real bug found while
pushing this project's formal pipeline against a genuinely complex real
design (YosysHQ/picorv32.v) for the first time, not just structural/CDC
scanning.

picorv32.v declares its RVFI ports (and several internal rvfi_* trace
registers) inside `ifdef RISCV_FORMAL`, a macro this project's own
formal backend never defines (SymbiYosys's `read -formal` convention
implicitly defines only `FORMAL`). Before this fix, analyze_rtl() never
resolved `ifdef` at all, so it included those ports in the parsed
RtlModule unconditionally -- generate_formal_wrapper() then built a DUT
instantiation connecting a port that a real, default synthesis build
(matching what sby's own read step actually produces) doesn't have,
producing a hard yosys error ("does not have a port named ...") on the
very first real property attempted against this design, not a proof-
complexity problem.

Fixed with strip_ifdef_blocks() (already built for cdc_check.py's own
structural scan) wired into analyze_rtl() itself, resolving every
`ifdef`/`ifndef` against {"FORMAL"} as the only macro assumed defined --
matching what this project's own pipeline actually compiles.
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

# A minimal synthetic reproduction of picorv32.v's own shape: a port
# declared only inside `ifdef RISCV_FORMAL`, which this project's own
# pipeline never defines, so it must NOT appear in the parsed module.
IFDEF_PORT_DESIGN = """
module core_with_conditional_port (
    input  wire        clk,
    input  wire        d,
    output reg         q,
`ifdef RISCV_FORMAL
    output reg  [63:0] rvfi_trace,
`endif
    output wire        done
);
    always @(posedge clk) q <= d;
`ifdef RISCV_FORMAL
    always @(posedge clk) rvfi_trace <= {63'd0, q};
`endif
    assign done = q;
endmodule
"""

# A port declared inside `ifdef FORMAL` (the one macro this project's
# own backend DOES define via SymbiYosys's `read -formal`) must be KEPT
# -- the inverse case, confirming the fix didn't just strip everything.
IFDEF_FORMAL_PORT_DESIGN = """
module core_with_formal_port (
    input  wire clk,
    input  wire d,
    output reg  q,
`ifdef FORMAL
    output wire past_valid,
`endif
    output wire done
);
    always @(posedge clk) q <= d;
`ifdef FORMAL
    assign past_valid = 1'b1;
`endif
    assign done = q;
endmodule
"""


def main() -> None:
    print("=== ifdef-guarded port for an UNDEFINED macro must NOT appear ===")
    mod = analyze_rtl(IFDEF_PORT_DESIGN, top_module="core_with_conditional_port")
    port_names = {p.name for p in mod.ports}
    print(f"  parsed ports: {sorted(port_names)}")
    assert "rvfi_trace" not in port_names, (
        "a port guarded by an undefined macro (RISCV_FORMAL) must be excluded", port_names
    )
    assert {"clk", "d", "q", "done"} <= port_names, port_names
    print("OK\n")

    print("=== ifdef-guarded port for FORMAL (the one macro this project's "
          "backend defines) must still appear ===")
    mod2 = analyze_rtl(IFDEF_FORMAL_PORT_DESIGN, top_module="core_with_formal_port")
    port_names2 = {p.name for p in mod2.ports}
    print(f"  parsed ports: {sorted(port_names2)}")
    assert "past_valid" in port_names2, (
        "a port guarded by `ifdef FORMAL` must be kept -- SymbiYosys's own "
        "`read -formal` convention defines FORMAL for every real proof run", port_names2
    )
    print("OK\n")

    picorv32_path = ROOT.parent / "external_rtl_cache" / "picorv32.v"
    if picorv32_path.is_file():
        print("=== Real: a genuine formal property against picorv32.v's actual RTL "
              "(the exact bug this fix was found chasing) ===")
        from api.main import formal_check  # noqa: E402 (local import: avoids app import cost above)

        source = picorv32_path.read_text(encoding="utf-8")
        mod3 = analyze_rtl(source, top_module="picorv32")
        port_names3 = {p.name for p in mod3.ports}
        assert "rvfi_csr_minstret_rdata" not in port_names3, (
            "the real picorv32.v's own RISCV_FORMAL-guarded port must be excluded too", None
        )

        props = json.dumps([
            {"name": "trap_is_registered", "expr": "trap == $past(trap) || trap != $past(trap)", "kind": "assert"},
        ])
        result = asyncio.run(formal_check(
            rtl_file=None, rtl_text=source, top_module="picorv32",
            properties=props, timeout_sec=30, depth_override=0,
            cross_check=False, blackbox_modules="", auto_blackbox=False,
        ))
        r = result["properties"][0]
        print(f"  verdict: {r['verdict']}")
        # A tautology ($past(x)==x or !=x is always true) proves this is a
        # real wrapper-build-and-run success, not a lucky FALSIFIED/ERROR
        # that happens not to touch the fixed ports -- the point here is
        # that the wrapper builds and a real solver run completes at all
        # against this design, which the original bug made impossible.
        assert r["verdict"] == "PROVEN", r
        print("OK\n")
    else:
        print(
            "(skipping real picorv32.v check -- fetch "
            "https://raw.githubusercontent.com/YosysHQ/picorv32/master/picorv32.v "
            f"to {picorv32_path} to include it)\n"
        )

    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
