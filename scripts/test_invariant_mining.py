"""Helper-invariant mining: simulation proposes, an induction run disposes.

Part 1 (static): the proposal rules, on synthetic recordings.
Part 2 (--solver): real Icarus + SymbiYosys. Must be run via PowerShell, not
Bash (see project memory on yosys subprocess spawning).

What Part 2 establishes:
  a. A true-but-not-inductive property (`q != 4'b0110` on a one-hot ring) is stuck
     under plain k-induction and closes once the mined invariants are
     assumed. The same invariants are what an engine-chain escalation uses.
  b. Disposal is exact: a candidate false in a reachable state is dropped
     (base case), and a true candidate that is not inductive on its own is
     dropped (induction step). Neither is ever used.
  c. Mining never hides a bug: on a ring whose reset value is wrong, the
     one-hot property is still FALSIFIED with mining on, and the one-hot
     candidate is not among the proven invariants.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from rtl_verify.invariant_mining import Candidate, MiningReport, propose_candidates  # noqa: E402

RING = """
module ring(input clk, input rst_n, input adv, output [3:0] q);
    reg [3:0] st;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) st <= 4'b0001;
        else if (adv) st <= {st[2:0], st[3]};
    assign q = st;
endmodule
"""

# same ring, wrong reset value: two bits set, so one-hot is genuinely false
BUGGY_RING = RING.replace("4'b0001", "4'b0011")


def part1_static() -> None:
    print("=== Part 1: proposal rules on synthetic recordings ===")
    cols = {
        "st": [1, 2, 4, 8, 1, 2, 4, 8],
        "cnt": [0, 1, 2, 3, 4, 3, 2, 1],
        "a": [0, 1, 0, 1, 0, 0, 1, 0],
        "b": [0, 0, 1, 0, 0, 1, 0, 0],
        "c": [0, 1, 1, 1, 0, 0, 1, 0],       # a -> c
        "mirror": [1, 2, 4, 8, 1, 2, 4, 8],  # identical recording to st
    }
    widths = {"st": 4, "cnt": 3, "a": 1, "b": 1, "c": 1, "mirror": 4}
    got = {(c.kind, c.expr) for c in propose_candidates(cols, widths)}
    for want in (
        ("onehot", "$onehot(st)"),
        ("upper_bound", "cnt <= 3'd4"),
        ("mutex", "!(a && b)"),
        ("implication", "!a || c"),
        ("equal", "st == mirror"),
    ):
        assert want in got, (want, sorted(got))
    assert not any("mirror" in e and k != "equal" for k, e in got), "aliased signal must not multiply candidates"
    assert ("value_set", "(st == 4'd1) || (st == 4'd2) || (st == 4'd4) || (st == 4'd8)") in got
    print(f"  {len(got)} candidates; onehot, bound, mutex, implication, alias-equality all present")
    capped = propose_candidates(cols, widths, limit=3)
    assert len(capped) == 3 and capped[0].kind == "value_set", [c.kind for c in capped]
    print("  limit respected; strongest kinds kept first")
    assert propose_candidates({"k": [5, 5, 5]}, {"k": 3}) == [], "a constant non-register carries no information"
    print("OK\n")


def part2_solver() -> None:
    from _formal_call import formal_check
    from rtl_verify.analyzer import analyze_rtl
    from rtl_verify.backends.symbiyosys import SymbiYosysBackend
    from rtl_verify.dut_probe import generate_probed_rtl
    from rtl_verify.formal_props import generate_formal_wrapper
    from rtl_verify.invariant_mining import _houdini, mine_invariants

    backend = SymbiYosysBackend()

    def probed(src: str, top: str):
        mod = analyze_rtl(src, top_module=top)
        psrc, pmod, _ = generate_probed_rtl(src, mod)
        work = Path(tempfile.mkdtemp(prefix="inv_test_"))
        path = work / "dut.v"
        path.write_text(psrc, encoding="utf-8")
        return psrc, pmod, path, work

    def kinduct(pmod, path, work, expr, extra=()):
        wrapper = work / f"w_{abs(hash(expr + str(extra))) % 10**6}.sv"
        wrapper.write_text(generate_formal_wrapper(pmod, list(extra) + [("p", expr, "assert")]), encoding="utf-8")
        return backend.run(path, wrapper, wrapper.with_suffix(""), top=f"{pmod.name}_formal_top",
                           depth=0, mode="prove", engine="smtbmc", timeout_sec=60).status

    print("=== Part 2a: mined invariants close a proof plain k-induction cannot ===")
    psrc, pmod, path, work = probed(RING, "ring")
    rep = mine_invariants(pmod, psrc, path, [], backend, work / "m")
    exprs = [c.expr for c in rep.proven]
    print(f"  status={rep.status} proposed={rep.proposed} proven={len(rep.proven)} dropped={len(rep.dropped)}")
    assert rep.status == "PROVEN_SET" and "$onehot(q)" in exprs, exprs
    plain = kinduct(pmod, path, work, "q != 4'b0110")
    helped = kinduct(pmod, path, work, "q != 4'b0110", rep.assumptions())
    print(f"  q != 0110 under k-induction alone: {plain}   with mined invariants: {helped}")
    assert plain == "UNKNOWN" and helped == "PASS", (plain, helped)
    print("OK\n")

    print("=== Part 2b: disposal is exact (false and non-inductive candidates never survive) ===")
    report = MiningReport()
    cands = [Candidate("q != 4'b0100", "user", origin="user"),     # reachable after two advances: false
             Candidate("q != 4'b0110", "user", origin="user")]     # true, but not inductive on its own
    _houdini(pmod, path, cands, [], backend, work / "h", 60, report)
    why = {c.expr: r for c, r in report.dropped}
    print(f"  status={report.status} proven={[c.expr for c in report.proven]}")
    for e, r in why.items():
        print(f"    dropped {e}: {r}")
    assert report.proven == [], report.proven
    assert "base case" in why["q != 4'b0100"], why
    assert "not inductive" in why["q != 4'b0110"], why
    print("OK\n")

    print("=== Part 2c: mining never hides a real bug ===")
    props = [{"name": "stays_onehot", "expr": "$onehot(q)", "kind": "assert"}]
    out = asyncio.run(formal_check(rtl_file=None, rtl_text=BUGGY_RING, top_module="ring",
                                   properties=json.dumps(props), timeout_sec=60, depth_override=0,
                                   cross_check=False, mine_invariants=True))
    verdict = out["properties"][0]["verdict"]
    proven = [c["expr"] for c in out["invariant_mining"]["proven"]]
    print(f"  buggy ring, mine_invariants=True: {verdict}   proven invariants: {proven}")
    assert verdict == "FALSIFIED", verdict
    assert not any("onehot" in e for e in proven), proven
    print("OK\n")

    print("=== Part 2d: end to end through formal_check, eager mode ===")
    props = [{"name": "never_0110", "expr": "q != 4'b0110", "kind": "assert"}]
    out = asyncio.run(formal_check(rtl_file=None, rtl_text=RING, top_module="ring",
                                   properties=json.dumps(props), timeout_sec=60, depth_override=0,
                                   cross_check=True, mine_invariants=True,
                                   invariant_candidates=json.dumps(["q != 4'b0100", "q <= 4'd9"])))
    r = out["properties"][0]
    inv = out["invariant_mining"]
    print(f"  verdict={r['verdict']} invariants_used={r['invariants_used']} proven={len(inv['proven'])} "
          f"dropped={[d['expr'] for d in inv['dropped']]}")
    assert r["verdict"] == "PROVEN" and r["invariants_used"] is True
    assert r["cross_check"]["performed"] is False
    assert any(d["expr"] == "q != 4'b0100" for d in inv["dropped"]), "a false user candidate must be dropped"
    assert any(c["expr"] == "q <= 4'd9" and c["origin"] == "user" for c in inv["proven"])
    bad = asyncio.run(formal_check(rtl_file=None, rtl_text=RING, top_module="ring",
                                   properties=json.dumps(props), timeout_sec=60, depth_override=0,
                                   invariant_candidates="not json"))
    assert "error" in bad and "invariant_candidates" in bad["error"], bad
    print("OK\n")

    print("=== Part 2e: auto_invariants escalation, with the engine chain limited to k-induction ===")
    import api.main as api_main
    original = api_main.recommended_engine_chain
    api_main.recommended_engine_chain = lambda *a, **k: [
        {"label": "k-induction (yices)", "mode": "prove", "engine": "smtbmc", "depth": 0}]
    try:
        stuck = asyncio.run(formal_check(rtl_file=None, rtl_text=RING, top_module="ring",
                                         properties=json.dumps(props), timeout_sec=60, depth_override=0,
                                         cross_check=False))["properties"][0]
        out = asyncio.run(formal_check(rtl_file=None, rtl_text=RING, top_module="ring",
                                       properties=json.dumps(props), timeout_sec=60, depth_override=0,
                                       cross_check=True, auto_invariants=True))
    finally:
        api_main.recommended_engine_chain = original
    r = out["properties"][0]
    info = r["auto_invariants"]
    print(f"  plain: {stuck['verdict']}   with auto_invariants: {r['verdict']}  resolved={info['resolved']} "
          f"proven={len(info['proven'])}")
    assert stuck["verdict"] in ("UNKNOWN", "TIMEOUT"), stuck["verdict"]
    assert r["verdict"] == "PROVEN" and info["resolved"] and r["invariants_used"] is True
    assert "Exact" in info["caveat"]
    print("OK\n")

    print("=== Part 2f: a real FIFO yields its occupancy bound ===")
    fifo = (ROOT / "examples" / "sync_fifo.v").read_text(encoding="utf-8")
    psrc, pmod, path, work = probed(fifo, "sync_fifo")
    rep = mine_invariants(pmod, psrc, path, [], backend, work / "m")
    exprs = [c.expr for c in rep.proven]
    print(f"  proven: {exprs}")
    assert "count <= 3'd4" in exprs and "!(full && empty)" in exprs, exprs
    print("OK\n")


if __name__ == "__main__":
    part1_static()
    if "--solver" in sys.argv:
        part2_solver()
    print("All requested parts passed.")
