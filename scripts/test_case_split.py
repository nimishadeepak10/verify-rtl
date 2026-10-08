"""Case-split completeness check, built from the two real false-positive
post-mortems in Seligman et al.'s "Formal Verification" Ch. 9 (a 6-bit
exponent split into 0..31 and 33..64, forgetting 32; a PCIe block verified
only in x8 mode). Needs real engines: run via PowerShell, not Bash.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from api.main import formal_case_split  # noqa: E402

# The bug sits exactly on the one value the split forgets, as in the book.
EXP_UNIT = """
module exp_unit(input [5:0] e, output [5:0] y);
    assign y = (e == 6'd32) ? 6'd0 : e;
endmodule
"""

MODE_UNIT = """
module mode_unit(input [1:0] mode, output [4:0] lanes);
    assign lanes = (mode == 2'd0) ? 5'd4 : (mode == 2'd1) ? 5'd8 : (mode == 2'd2) ? 5'd16 : 5'd0;
endmodule
"""

SEQ_UNIT = """
module seq_unit(input clk, input rst_n, input [1:0] mode, output reg [7:0] r);
    always @(posedge clk or negedge rst_n)
        if (!rst_n) r <= 8'd0; else r <= r | {6'b0, mode};
endmodule
"""


def call(source, top, cases, props):
    return asyncio.run(formal_case_split(
        rtl_file=None, rtl_text=source, top_module=top, cases=json.dumps(cases),
        properties=json.dumps(props), timeout_sec=60, depth_override=0))


def main() -> None:
    print("=== 1: the book's exponent bug: every run is green, the split is still incomplete ===")
    prop = [{"name": "y_is_e", "expr": "y == e", "kind": "assert"}]
    cases = [{"name": "low", "expr": "e <= 6'd31"}, {"name": "high", "expr": "e >= 6'd33"}]
    out = call(EXP_UNIT, "exp_unit", cases, prop)
    print(f"  verdict={out['verdict']}  completeness={out['completeness']['status']} "
          f"example={out['completeness']['uncovered_example']}")
    print(f"  per-case: {[(c['name'], c['property_verdicts']) for c in out['cases']]}")
    assert all(c["property_verdicts"]["y_is_e"] == "PROVEN" for c in out["cases"]), out["cases"]
    assert out["completeness"]["status"] == "GAP", out["completeness"]
    assert out["completeness"]["uncovered_example"] == {"e": 32}, out["completeness"]
    assert out["verdict"] == "SPLIT_INCOMPLETE", out["verdict"]
    print("OK\n")

    print("=== 2: adding the forgotten case completes the split AND exposes the real bug ===")
    cases3 = cases + [{"name": "boundary", "expr": "e == 6'd32"}]
    out = call(EXP_UNIT, "exp_unit", cases3, prop)
    print(f"  verdict={out['verdict']}  completeness={out['completeness']['status']}")
    print(f"  per-case: {[(c['name'], c['property_verdicts']) for c in out['cases']]}")
    assert out["completeness"]["status"] == "COMPLETE"
    verdicts = {c["name"]: c["property_verdicts"]["y_is_e"] for c in out["cases"]}
    assert verdicts == {"low": "PROVEN", "high": "PROVEN", "boundary": "FALSIFIED"}, verdicts
    assert out["verdict"] == "SPLIT_FALSIFIED"
    print("OK\n")

    print("=== 3: the PCIe-style mode gap; a global assumption legitimately closes it ===")
    modes = [{"name": "x4", "expr": "mode == 2'd0"}, {"name": "x8", "expr": "mode == 2'd1"},
             {"name": "x16", "expr": "mode == 2'd2"}]
    p = [{"name": "lanes_nonzero", "expr": "lanes != 5'd0", "kind": "assert"}]
    out = call(MODE_UNIT, "mode_unit", modes, p)
    print(f"  no assumption:  completeness={out['completeness']['status']} example={out['completeness']['uncovered_example']}")
    assert out["completeness"]["status"] == "GAP" and out["completeness"]["uncovered_example"] == {"mode": 3}
    out = call(MODE_UNIT, "mode_unit", modes, p + [{"name": "legal", "expr": "mode != 2'd3", "kind": "assume"}])
    print(f"  mode != 3:      completeness={out['completeness']['status']} verdict={out['verdict']}")
    assert out["completeness"]["status"] == "COMPLETE" and out["verdict"] == "SPLIT_PROVEN", out
    print("OK\n")

    print("=== 4: a case the global assumptions already rule out is flagged as empty ===")
    modes_plus = modes + [{"name": "x32", "expr": "mode == 2'd3"}]
    out = call(MODE_UNIT, "mode_unit", modes_plus,
               [{"name": "legal", "expr": "mode != 2'd3", "kind": "assume"}])
    empty = [c["name"] for c in out["cases"] if c["reachable"] is False]
    print(f"  empty cases: {empty}  verdict={out['verdict']}")
    assert empty == ["x32"] and out["verdict"] == "SPLIT_INCOMPLETE", out
    print("OK\n")

    print("=== 5: sequential design, per-cycle coverage vs. sequence safety ===")
    seq_cases = [{"name": f"m{i}", "expr": f"mode == 2'd{i}"} for i in range(4)]
    sp = [{"name": "upper_clear", "expr": "(r & 8'hFC) == 8'd0", "kind": "assert"}]
    out = call(SEQ_UNIT, "seq_unit", seq_cases, sp)
    print(f"  free mode:   sequence={out['sequence_safety']['status']} varying={out['sequence_safety']['varying_signals']} verdict={out['verdict']}")
    assert out["sequence_safety"]["status"] == "PER_CYCLE_ONLY" and out["sequence_safety"]["varying_signals"] == ["mode"]
    assert out["verdict"] == "SPLIT_PROVEN_PER_CYCLE_ONLY", out["verdict"]
    out = call(SEQ_UNIT, "seq_unit", seq_cases,
               sp + [{"name": "held", "expr": "mode == $past(mode)", "kind": "assume"}])
    print(f"  mode held:   sequence={out['sequence_safety']['status']} verdict={out['verdict']}")
    assert out["sequence_safety"]["status"] == "SEQUENCE_SAFE" and out["verdict"] == "SPLIT_PROVEN", out
    print("OK\n")

    print("=== 6: bad input is an error ===")
    err = call(EXP_UNIT, "exp_unit", [{"name": "only", "expr": "e == 1"}], prop)
    assert "error" in err, err
    print(f"  {err['error']}")
    print("OK\n")
    print("All case-split tests passed.")


if __name__ == "__main__":
    main()
