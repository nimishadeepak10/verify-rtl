"""Counter abstraction (adapted from Seligman et al.'s "Formal Verification"
Ch.10, Siemens Verification Horizons' complexity series Part 4, and the FVM
complexity guide).

Parts 1-2 are static. Part 3 (--solver) needs real engines and must be run
via PowerShell, not Bash (see project memory on yosys spawning).
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
from rtl_verify.counter_abstract import abstract_counters, find_counter_candidates  # noqa: E402

TIMEOUT_CTRL = """
module timeout_ctrl (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        start,
    output reg  [1:0]  state,
    output reg         timed_out,
    output reg  [31:0] cnt
);
    localparam IDLE = 2'd0, RUN = 2'd1, DONE = 2'd2;
    localparam LIMIT = 32'd1000000;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= IDLE; timed_out <= 1'b0; cnt <= 32'd0;
        end else case (state)
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

SMALL = TIMEOUT_CTRL.replace("32'd1000000", "32'd20")


def part1_static() -> None:
    print("=== Part 1: candidate discovery ===")
    mod = analyze_rtl(TIMEOUT_CTRL, top_module="timeout_ctrl")
    cands = find_counter_candidates(mod, TIMEOUT_CTRL)
    assert [c.signal for c in cands] == ["cnt"], cands
    c = cands[0]
    print(f"  {c.signal}: width={c.width} thresholds={c.thresholds} increments={c.increments} observable={c.observable}")
    assert c.width == 32 and c.thresholds == [1000000] and c.increments == 1 and c.observable
    assert "cnt <= 32'd0" not in str(c.thresholds)  # a statement-level `<=` is never a comparison
    print("OK\n")


def part2_rewrite() -> None:
    print("=== Part 2: rewrite shape and refusals ===")
    new, report, errors = abstract_counters(TIMEOUT_CTRL, "timeout_ctrl", ["cnt"])
    assert not errors and report[0]["increments_rewritten"] == 1, (report, errors)
    assert "cnt <= __cabs_inc_cnt;" in new, new
    assert "(* anyseq *) wire __cabs_jump_cnt;" in new
    assert "cnt <= 32'd0" in new and new.count("cnt <= 32'd0") == 2, "clears/resets must be left alone"
    assert "32'd999999" in new, "jump target is one step BEFORE the critical value"
    decl_pos = new.index("output reg  [31:0] cnt")
    assert new.index("__cabs_inc_cnt =") > decl_pos, "helper must come after the counter's declaration"
    print("  increment rewritten; resets/clears untouched; jump target = threshold - 1")

    _, _, errs = abstract_counters(TIMEOUT_CTRL, "timeout_ctrl", ["state", "ghost"])
    assert len(errs) == 2, errs
    print(f"  refused: {errs}")
    print("OK\n")


def part3_solver() -> None:
    from _formal_call import formal_check

    def call(source, props, **kw):
        args = dict(rtl_file=None, rtl_text=source, top_module="timeout_ctrl",
                    properties=json.dumps(props), timeout_sec=40, depth_override=0, cross_check=True,
                    blackbox_modules="", auto_blackbox=False, cut_signals="", auto_cutpoint=False,
                    decompose=False, rom_to_case=False, param_overrides="", auto_param_reduction=False,
                    counter_abstraction="", auto_counter_abstraction=False)
        args.update(kw)
        return asyncio.run(formal_check(**args))

    deep_bug = [{"name": "never_timeout", "expr": "timed_out == 1'b0", "kind": "assert"}]
    true_prop = [{"name": "timeout_implies_done", "expr": "!timed_out || (state == 2'd2)", "kind": "assert"}]

    print("=== Part 3a: a bug 1,000,000 cycles deep is out of reach plain ===")
    plain = call(TIMEOUT_CTRL, deep_bug)["properties"][0]
    print(f"  plain: {plain['verdict']}  attempts={[(a['label'], a['status']) for a in plain['attempts']]}")
    assert plain["verdict"] in ("TIMEOUT", "UNKNOWN"), plain["verdict"]
    print("OK\n")

    print("=== Part 3b: manual counter_abstraction finds it ===")
    man_out = call(TIMEOUT_CTRL, deep_bug, counter_abstraction="cnt")
    man = man_out["properties"][0]
    print(f"  abstracted: {man['verdict']}  cross_check_performed={man['cross_check']['performed']}")
    assert man["verdict"] == "FALSIFIED" and man["cross_check"]["performed"] is False
    assert "COUNTER ABSTRACTION" in man_out["counter_abstraction"]["caveat"]
    print("OK\n")

    print("=== Part 3c: auto_counter_abstraction rescues the TIMEOUT by itself ===")
    auto = call(TIMEOUT_CTRL, deep_bug, auto_counter_abstraction=True)["properties"][0]
    info = auto["auto_counter_abstraction"]
    print(f"  auto: {auto['verdict']}  {json.dumps(info)[:240]}")
    assert auto["verdict"] == "FALSIFIED" and info["resolved"], info
    assert [c["signal"] for c in info["counters"]] == ["cnt"]
    print("OK\n")

    print("=== Part 3d: soundness check on a SMALL limit where plain is cheap ===")
    for label, props, want in (("true property", true_prop, "PROVEN"), ("false property", deep_bug, "FALSIFIED")):
        p = call(SMALL, props, cross_check=False)["properties"][0]["verdict"]
        a = call(SMALL, props, cross_check=False, counter_abstraction="cnt")["properties"][0]["verdict"]
        print(f"  {label}: plain={p} abstracted={a}")
        assert p == a == want, (label, p, a)
    print("OK\n")

    print("=== Part 3e: a true property stays PROVEN on the 1,000,000-cycle design ===")
    t = call(TIMEOUT_CTRL, true_prop, cross_check=False, counter_abstraction="cnt")["properties"][0]
    print(f"  abstracted: {t['verdict']}")
    assert t["verdict"] == "PROVEN"
    print("OK\n")

    print("=== Part 3f: bad counter names are errors ===")
    err = call(TIMEOUT_CTRL, true_prop, counter_abstraction="ghost")
    assert "error" in err and "ghost" in err["error"], err
    print(f"  {err['error']}")
    print("OK\n")


if __name__ == "__main__":
    part1_static()
    part2_rewrite()
    if "--solver" in sys.argv:
        part3_solver()
    print("All requested parts passed.")
