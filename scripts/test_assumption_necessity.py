"""Assumption necessity: which assumptions did a PROVEN property need?

Part 1 (static): the across-property summary.
Part 2 (--solver): real SymbiYosys. Must be run via PowerShell, not Bash (see
project memory on yosys subprocess spawning).

The test design is `y <= a & b` with the property `!y` (y is never high).
Three assumptions are supplied:
    A1: !a                 excludes every y=1 scenario
    A2: !b                 excludes every y=1 scenario too (an equivalent cause)
    A3: a || !a            a tautology; excludes nothing
The proof needs one of A1/A2, and neither alone is special, so the honest
answer is order-dependent: tried in order, A1 is dropped (A2 still suffices),
then A2 is needed, and A3 is never needed. The test pins exactly that,
because it is the subtle case the module's docstring warns about.
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

from rtl_verify.assumption_necessity import (  # noqa: E402
    AssumptionRole, NecessityReport, summarize,
)

GATE = """
module gate(input clk, input rst_n, input a, input b, output reg y);
    always @(posedge clk or negedge rst_n)
        if (!rst_n) y <= 1'b0;
        else y <= a & b;
endmodule
"""

A1 = ("A1", "!a", "assume")
A2 = ("A2", "!b", "assume")
A3 = ("A3", "a || !a", "assume")


def part1_static() -> None:
    print("=== Part 1: across-property summary ===")
    r1 = NecessityReport("p1", "OK", roles=[
        AssumptionRole("A1", "!a", "NEEDED"), AssumptionRole("A2", "!b", "NOT_NEEDED_GIVEN_REST"),
        AssumptionRole("A3", "x", "NOT_NEEDED_GIVEN_REST")], runs=3)
    r2 = NecessityReport("p2", "OK", roles=[
        AssumptionRole("A1", "!a", "NOT_NEEDED_GIVEN_REST"), AssumptionRole("A2", "!b", "NEEDED"),
        AssumptionRole("A3", "x", "NOT_NEEDED_GIVEN_REST")], runs=3)
    s = summarize([r1, r2], [A1, A2, A3])
    print(f"  needed_by={s['needed_by']}  never={s['needed_by_no_checked_property']}")
    assert s["needed_by"] == {"A1": ["p1"], "A2": ["p2"], "A3": []}
    assert s["needed_by_no_checked_property"] == ["A3"]
    assert r1.proof_core == ["A1"] and r1.not_needed == ["A2", "A3"]
    na = NecessityReport("p3", "NOT_APPLICABLE")
    s = summarize([na], [A1])
    assert s["needed_by_no_checked_property"] == [], "no checked property means no claim about unused assumptions"
    print("OK\n")


def part2_solver() -> None:
    from _formal_call import formal_check
    from rtl_verify.analyzer import analyze_rtl
    from rtl_verify.assumption_necessity import check_assumption_necessity
    from rtl_verify.backends.symbiyosys import SymbiYosysBackend

    backend = SymbiYosysBackend()
    mod = analyze_rtl(GATE, top_module="gate")
    work = Path(tempfile.mkdtemp(prefix="necessity_test_"))
    rtl = work / "gate.v"
    rtl.write_text(GATE, encoding="utf-8")

    def run(assumes, **kw):
        return check_assumption_necessity(mod, rtl, backend, assumes, ("never_y", "!y"),
                                          timeout_sec=60, work_root=work / f"r{abs(hash(str(assumes) + str(kw))) % 10**6}", **kw)

    print("=== Part 2a: equivalent causes and a tautology ===")
    rep = run([A1, A2, A3])
    roles = {r.name: r.role for r in rep.roles}
    print(f"  roles={roles}  core={rep.proof_core}  runs={rep.runs}")
    assert roles == {"A1": "NOT_NEEDED_GIVEN_REST", "A2": "NEEDED", "A3": "NOT_NEEDED_GIVEN_REST"}, roles
    assert rep.proof_core == ["A2"] and rep.runs == 3 and "order" in rep.note
    print("OK\n")

    print("=== Part 2b: a needed assumption comes with the behavior it excludes ===")
    rep = run([A1, A3])
    a1 = next(r for r in rep.roles if r.name == "A1")
    print(f"  A1 role={a1.role}  excluded_behavior={a1.excluded_behavior}")
    assert a1.role == "NEEDED" and rep.proof_core == ["A1"]
    assert a1.excluded_behavior and "a" in a1.excluded_behavior
    assert any(v == "1" for _t, v in a1.excluded_behavior["a"]), \
        "the counterexample must show a driven high, the very behavior A1 forbids"
    print("OK\n")

    print("=== Part 2c: cap and empty set are reported, not hidden ===")
    rep = run([A1, A2, A3], max_runs=1)
    assert rep.status == "INCONCLUSIVE" and rep.runs == 1, (rep.status, rep.runs)
    assert "A2" in rep.proof_core and "A3" in rep.proof_core, "unchecked assumptions stay in the core"
    none = run([])
    assert none.status == "NOT_APPLICABLE"
    print(f"  capped: {rep.status} after {rep.runs} run; empty set: {none.status}")
    print("OK\n")

    print("=== Part 2d: end to end through formal_check ===")
    props = [{"name": "A1", "expr": "!a", "kind": "assume"},
             {"name": "A2", "expr": "!b", "kind": "assume"},
             {"name": "A3", "expr": "a || !a", "kind": "assume"},
             {"name": "never_y", "expr": "!y", "kind": "assert"},
             {"name": "y_follows_a", "expr": "!y || a", "kind": "assert"}]
    out = asyncio.run(formal_check(rtl_file=None, rtl_text=GATE, top_module="gate",
                                   properties=json.dumps(props), timeout_sec=60, depth_override=0,
                                   cross_check=False, check_assumptions=True))
    summ = out["assumption_necessity"]
    print(f"  checked={summ['properties_checked']}  never_needed={summ['needed_by_no_checked_property']}")
    assert set(summ["properties_checked"]) == {"never_y", "y_follows_a"}
    assert "A3" in summ["needed_by_no_checked_property"]
    by_name = {p["name"]: p for p in out["properties"]}
    core = by_name["never_y"]["assumption_necessity"]["proof_core"]
    assert core == ["A2"], core
    off = asyncio.run(formal_check(rtl_file=None, rtl_text=GATE, top_module="gate",
                                   properties=json.dumps(props), timeout_sec=60, depth_override=0,
                                   cross_check=False))
    assert off["assumption_necessity"] is None
    print("OK\n")

    print("=== Part 2e: a FALSIFIED property is not checked ===")
    props2 = [{"name": "A3", "expr": "a || !a", "kind": "assume"},
              {"name": "never_y", "expr": "!y", "kind": "assert"}]
    out = asyncio.run(formal_check(rtl_file=None, rtl_text=GATE, top_module="gate",
                                   properties=json.dumps(props2), timeout_sec=60, depth_override=0,
                                   cross_check=False, check_assumptions=True))
    r = out["properties"][0] if out["properties"][0]["name"] == "never_y" else out["properties"][-1]
    print(f"  verdict={r['verdict']}  necessity block present: {'assumption_necessity' in r}")
    assert r["verdict"] == "FALSIFIED" and "assumption_necessity" not in r
    assert out["assumption_necessity"]["note"].startswith("no PROVEN property")
    print("OK\n")


if __name__ == "__main__":
    part1_static()
    if "--solver" in sys.argv:
        part2_solver()
    print("All requested parts passed.")
