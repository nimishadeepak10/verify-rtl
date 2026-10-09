"""Bounded forward-progress checking (adapted from LUBIS EDA's deadlock /
livelock / starvation article). Part 1 is static; Part 2 (--solver) uses real
SymbiYosys and must be run via PowerShell, not Bash.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.dut_probe import generate_probed_rtl  # noqa: E402
from rtl_verify.progress_check import (  # noqa: E402
    Fairness, ProgressChecker, ProgressSpec, build_progress_wrapper,
)

HANDSHAKE = """
module hs(input clk, input rst_n, input req, output done);
    reg [1:0] st;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) st <= 2'd0;
        else case (st)
            2'd0: if (req) st <= 2'd1;
            2'd1: st <= 2'd2;
            2'd2: st <= 2'd0;
            default: st <= 2'd0;
        endcase
    assign done = (st == 2'd2);
endmodule
"""

# waits for a credit that only the state it is waiting to leave produces
STUCK = """
module stuck(input clk, input rst_n, input req, output done);
    reg [1:0] st;
    reg credit;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) begin st <= 2'd0; credit <= 1'b0; end
        else case (st)
            2'd0: if (req) st <= 2'd1;
            2'd1: if (credit) st <= 2'd2;
            2'd2: begin credit <= 1'b1; st <= 2'd0; end
            default: st <= 2'd0;
        endcase
    assign done = (st == 2'd2);
endmodule
"""

# bounces between two states and never reaches the completing one
SPIN = """
module spin(input clk, input rst_n, input req, output done);
    reg [1:0] st;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) st <= 2'd0;
        else case (st)
            2'd0: if (req) st <= 2'd1;
            2'd1: st <= 2'd2;
            2'd2: st <= 2'd1;
            default: st <= 2'd0;
        endcase
    assign done = (st == 2'd3);
endmodule
"""

FIXED = """
module fixedarb(input clk, input rst_n, input [3:0] req, output reg [3:0] grant);
    always @(posedge clk or negedge rst_n)
        if (!rst_n) grant <= 4'd0;
        else grant <= req[0] ? 4'b0001 : req[1] ? 4'b0010 : req[2] ? 4'b0100 : req[3] ? 4'b1000 : 4'b0000;
endmodule
"""


def _prep(src, top):
    mod = analyze_rtl(src, top_module=top)
    psrc, pmod, _ = generate_probed_rtl(src, mod)
    work = Path(tempfile.mkdtemp(prefix=f"progress_{top}_"))
    path = work / "dut.v"
    path.write_text(psrc, encoding="utf-8")
    return pmod, path, work


def part1_static() -> None:
    print("=== Part 1: monitor construction ===")
    pmod, _path, _w = _prep(HANDSHAKE, "hs")
    text = build_progress_wrapper(pmod, [ProgressSpec("p", "req", "done", 5)],
                                  [Fairness("f", "!stall", 3)])
    assert "reg [2:0] __wait_0" in text, "bound 5 saturates at 6, which needs 3 bits"
    assert "__wait_0 <= 3'd5" in text
    assert "__fair_0 <= 3'd2" in text            # within 3 -> a false run of at most 2
    assert text.index("__pend_0") < text.index("`ifdef FORMAL")
    off = build_progress_wrapper(pmod, [ProgressSpec("p", "req", "done", 5)], [Fairness("f", "1", 3)],
                                 active_fairness=[])
    assert "fair_0: assume" not in off, "a deactivated fairness assumption must not be applied"
    try:
        build_progress_wrapper(analyze_rtl("module c(input a, output b); assign b = a; endmodule", "c"),
                               [ProgressSpec("p", "a", "b", 2)], [])
    except ValueError as e:
        print(f"  combinational design refused: {e}")
    else:
        raise AssertionError("a clockless design has no cycles to bound")
    print("OK\n")


def part2_solver() -> None:
    from rtl_verify.backends.symbiyosys import SymbiYosysBackend
    backend = SymbiYosysBackend()

    print("=== Part 2a: a bound is exact (proven at N, falsified at N-1) ===")
    pmod, path, w = _prep(HANDSHAKE, "hs")
    ck = ProgressChecker(pmod, path, backend, w / "p", timeout_sec=60)
    r2 = ck.check(ProgressSpec("resp", "req", "done", 2))
    r1 = ck.check(ProgressSpec("resp", "req", "done", 1))
    mb = ck.find_min_bound(ProgressSpec("resp", "req", "done", 1), max_bound=8)
    print(f"  N=2: {r2.verdict}   N=1: {r1.verdict}   min bound: {mb['min_bound']} ({mb['status']})")
    assert r2.verdict == "PROVEN" and r1.verdict == "FALSIFIED" and mb["min_bound"] == 2
    print("OK\n")

    print("=== Part 2b: a frozen state is labelled deadlock-like ===")
    pmod, path, w = _prep(STUCK, "stuck")
    ck = ProgressChecker(pmod, path, backend, w / "p", timeout_sec=60)
    r = ck.check(ProgressSpec("resp", "req", "done", 5))
    print(f"  {r.verdict} {r.classification}  changed={r.evidence.get('outputs_changed_while_waiting')}")
    assert r.verdict == "FALSIFIED" and r.classification == "DEADLOCK_LIKE", (r.verdict, r.classification)
    assert "heuristic" in r.note
    print("OK\n")

    print("=== Part 2c: activity without completion is labelled livelock-like ===")
    pmod, path, w = _prep(SPIN, "spin")
    ck = ProgressChecker(pmod, path, backend, w / "p", timeout_sec=60)
    r = ck.check(ProgressSpec("resp", "req", "done", 5))
    print(f"  {r.verdict} {r.classification}  changed={r.evidence.get('outputs_changed_while_waiting')}")
    assert r.verdict == "FALSIFIED" and r.classification == "LIVELOCK_LIKE", (r.verdict, r.classification)
    print("OK\n")

    print("=== Part 2d: starvation needs a fairness assumption, and the check says which were needed ===")
    pmod, path, w = _prep(FIXED, "fixedarb")
    ck = ProgressChecker(pmod, path, backend, w / "p", timeout_sec=60)
    # sticky=False: the request must be HELD to count as waiting; an environment that
    # withdraws it exactly when it would be served is not starving anything.
    spec = ProgressSpec("req3_served", "req[3]", "grant[3]", 4, competing="|grant[2:0]", sticky=False)
    plain = ck.check(spec)
    nb = ck.find_min_bound(spec, max_bound=8)
    print(f"  no fairness: {plain.verdict} {plain.classification}; bound search: {nb['status']}")
    assert plain.verdict == "FALSIFIED" and plain.classification == "STARVATION", (plain.verdict, plain.classification)
    assert nb["status"] == "NO_BOUND_UP_TO_CAP" and nb["min_bound"] is None
    fair = [Fairness("higher_priority_pauses", "!(req[0] || req[1] || req[2])", 3),
            Fairness("vacuous", "1'b1", 5)]
    fixed = ck.check(spec, fair)
    fb = ck.find_min_bound(spec, fair, max_bound=8)
    print(f"  with fairness: {fixed.verdict}; needed={fixed.needed_fairness} unneeded={fixed.unneeded_fairness}; "
          f"min bound={fb['min_bound']}")
    assert fixed.verdict == "PROVEN" and fixed.needed_fairness == ["higher_priority_pauses"]
    assert fixed.unneeded_fairness == ["vacuous"]
    assert fb["status"] == "FOUND" and 1 <= fb["min_bound"] <= 4
    print("OK\n")

    print("=== Part 2e: a real arbiter: backpressure can starve it, bounded backpressure cannot ===")
    src = (ROOT / "examples" / "arbiter4.v").read_text(encoding="utf-8")
    pmod, path, w = _prep(src, "arbiter4")
    ck = ProgressChecker(pmod, path, backend, w / "p", timeout_sec=90)
    spec = ProgressSpec("req2_granted", "req[2]", "grant[2]", 12, competing="|(grant & 4'b1011)", sticky=False)
    free = ck.check(spec)
    fair = [Fairness("busy_drops", "!busy_block", 3)]
    ok = ck.check(spec, fair)
    print(f"  busy_block free: {free.verdict} {free.classification}; busy_block drops every 3: {ok.verdict}"
          f" needed={ok.needed_fairness}")
    assert free.verdict == "FALSIFIED"
    assert ok.verdict == "PROVEN" and ok.needed_fairness == ["busy_drops"]
    print("OK\n")

    print("=== Part 2f: the endpoint, with a bound search ===")
    import asyncio
    import json
    import api.main as api_main
    out = asyncio.run(api_main.formal_progress(
        rtl_file=None, rtl_text=src, top_module="arbiter4",
        specs=json.dumps([{"name": "req2", "request": "req[2]", "response": "grant[2]", "bound": 12,
                           "competing": "|(grant & 4'b1011)", "sticky": False}]),
        fairness=json.dumps([{"name": "busy_drops", "expr": "!busy_block", "within": 3}]),
        properties="[]", find_bound=True, max_bound=16, timeout_sec=90, depth_override=0))
    r = out["results"][0]
    print(f"  verdict={r['verdict']} min bound={r['bound_search']['min_bound']} runs={out['solver_runs']}")
    assert r["verdict"] == "PROVEN" and r["bound_search"]["status"] == "FOUND"
    assert 1 <= r["bound_search"]["min_bound"] <= 12 and r["needed_fairness"] == ["busy_drops"]
    bad = asyncio.run(api_main.formal_progress(
        rtl_file=None, rtl_text=src, top_module="arbiter4", specs=json.dumps([{"name": "x"}]),
        fairness="[]", properties="[]", find_bound=False, max_bound=8, timeout_sec=30, depth_override=0))
    assert "error" in bad
    print("OK\n")


if __name__ == "__main__":
    part1_static()
    if "--solver" in sys.argv:
        part2_solver()
    print("All requested parts passed.")
