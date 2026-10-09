"""Strategy planner: the right techniques for the right design and goal.

No solver is run; the planner is recommend-only. The point of the test is
selectivity: a plan must skip what a design has no structure for, and must
change with the goal where the goal changes what an answer means.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from rtl_verify.strategy_planner import GOALS, plan_strategy  # noqa: E402
import test_counter_abstraction as tca  # noqa: E402
import test_cutpoint_and_decompose as tcd  # noqa: E402
import test_data_independence as tdi  # noqa: E402
import test_param_reduce_and_rom as tpr  # noqa: E402

RING = """
module ring(input clk, input rst_n, input adv, output [3:0] q);
    reg [3:0] st;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) st <= 4'b0001;
        else if (adv) st <= {st[2:0], st[3]};
    assign q = st;
endmodule
"""

VOCAB = {"RUN_FIRST", "RECOMMENDED", "IF_STUCK", "AFTER_PROOF", "OPTIONAL", "NOT_NEEDED",
         "NOT_ACCEPTABLE_FOR_GOAL", "NOT_APPLICABLE"}
ONEHOT = [{"name": "onehot", "expr": "$onehot(q)", "kind": "assert"}]


def decisions(plan):
    return {s.technique: s.decision for s in plan.steps}


def active(plan):
    return [s.technique for s in plan.steps if s.decision in ("RECOMMENDED", "IF_STUCK", "AFTER_PROOF")]


def main() -> None:
    print("=== 1: a small single-module FSM gets almost nothing ===")
    p = plan_strategy(RING, "ring", "prove", ONEHOT)
    d = decisions(p)
    print(f"  size={p.facts['size_class']}  active={active(p)}")
    for t in ("black_boxing", "cut_points", "counter_abstraction", "rom_to_case",
              "parameter_reduction", "data_width_reduction", "case_split", "progress_check"):
        assert d[t] == "NOT_APPLICABLE", (t, d[t])
    assert d["plain_proof"] == "RUN_FIRST" and p.steps[0].technique == "plain_proof"
    assert active(p) == ["helper_invariants"], active(p)
    bb = p.by_technique("black_boxing")
    assert "Single module" in bb.reason
    print("OK\n")

    print("=== 2: a large hierarchical design with a wide accumulator ===")
    big = tcd._wrapper_with_accumulator()
    p = plan_strategy(big, "big_soc_wrapper", "prove",
                      [{"name": "g", "expr": "(grant == 4'd0) || $onehot(grant)", "kind": "assert"}])
    d = decisions(p)
    print(f"  size={p.facts['size_class']} bits={p.facts['state_bits_estimate']}  active={active(p)}")
    assert p.facts["hierarchical"] and "wide_mac_pipeline" in p.facts["submodules"]
    assert d["black_boxing"] in ("IF_STUCK", "RECOMMENDED")
    assert p.by_technique("black_boxing").apply_with["blackbox_modules"] == "wide_mac_pipeline"
    assert d["cut_points"] in ("IF_STUCK", "RECOMMENDED")
    assert "mac_hash" in p.by_technique("cut_points").apply_with["cut_signals"]
    assert d["parameter_reduction"] == "NOT_ACCEPTABLE_FOR_GOAL", d["parameter_reduction"]
    pb = plan_strategy(big, "big_soc_wrapper", "find_bugs",
                       [{"name": "g", "expr": "(grant == 4'd0) || $onehot(grant)", "kind": "assert"}])
    assert decisions(pb)["parameter_reduction"] in ("RECOMMENDED", "NOT_NEEDED")
    assert decisions(pb)["parameter_reduction"] == "RECOMMENDED"
    print(f"  parameter_reduction: prove -> {d['parameter_reduction']}, find_bugs -> "
          f"{decisions(pb)['parameter_reduction']}")
    print("OK\n")

    print("=== 3: counter abstraction depends on the goal ===")
    pt = plan_strategy(tca.TIMEOUT_CTRL, "timeout_ctrl", "prove",
                       [{"name": "p", "expr": "!timed_out || (state == 2'd2)", "kind": "assert"}])
    pf = plan_strategy(tca.TIMEOUT_CTRL, "timeout_ctrl", "find_bugs",
                       [{"name": "p", "expr": "!timed_out || (state == 2'd2)", "kind": "assert"}])
    print(f"  prove -> {decisions(pt)['counter_abstraction']}   find_bugs -> {decisions(pf)['counter_abstraction']}")
    assert decisions(pt)["counter_abstraction"] == "IF_STUCK"
    assert decisions(pf)["counter_abstraction"] == "RECOMMENDED"
    assert pt.by_technique("counter_abstraction").apply_with["counter_abstraction"] == "cnt"
    assert "FALSIFIED" in pt.by_technique("counter_abstraction").soundness
    print("OK\n")

    print("=== 4: ROMs are converted only when provably constant ===")
    pr = plan_strategy(tpr.ROM_OK, "rom_lookup", "prove", [])
    pm = plan_strategy(tpr.RAM, "ram1", "prove", [])
    print(f"  ROM -> {decisions(pr)['rom_to_case']}   RAM -> {decisions(pm)['rom_to_case']}")
    assert decisions(pr)["rom_to_case"] == "RECOMMENDED"
    assert pr.by_technique("rom_to_case").soundness == "EXACT"
    assert decisions(pm)["rom_to_case"] == "NOT_APPLICABLE"
    print("OK\n")

    print("=== 5: data-width reduction needs independence, and a property that may use it ===")
    pi = plan_strategy(tdi.PFIFO, "pfifo", "prove",
                       [{"name": "c", "expr": "!(full && empty)", "kind": "assert"}])
    pd = plan_strategy(tdi.MAGIC, "magic_fifo", "prove",
                       [{"name": "c", "expr": "!overflow", "kind": "assert"}])
    pa = plan_strategy(tdi.PFIFO, "pfifo", "prove",
                       [{"name": "c", "expr": "rd_data <= wr_data", "kind": "assert"}])
    print(f"  independent -> {decisions(pi)['data_width_reduction']}   dependent -> "
          f"{decisions(pd)['data_width_reduction']}   arithmetic property -> {decisions(pa)['data_width_reduction']}")
    assert decisions(pi)["data_width_reduction"] in ("IF_STUCK", "RECOMMENDED")
    assert pi.by_technique("data_width_reduction").apply_with["data_width_reduction"] is True
    assert decisions(pd)["data_width_reduction"] == "NOT_APPLICABLE"
    assert pd.by_technique("data_width_reduction").evidence, "a refusal must cite where data reaches control"
    assert decisions(pa)["data_width_reduction"] == "NOT_ACCEPTABLE_FOR_GOAL"
    print("OK\n")

    print("=== 6: properties and assumptions drive the checks around a proof ===")
    props = [{"name": "a1", "expr": "!(adv == 1'b0) || (q == $past(q))", "kind": "assume"},
             {"name": "both", "expr": "!(adv || !adv) || ($onehot(q) && q != 4'd0)", "kind": "assert"}]
    p = plan_strategy(RING, "ring", "signoff", props)
    d = decisions(p)
    print(f"  assumption_necessity={d['assumption_necessity']}  decomposition={d['assertion_decomposition']}  "
          f"strength={d['property_strength_checks']}")
    assert d["assumption_necessity"] == "AFTER_PROOF"
    assert d["assertion_decomposition"] == "RECOMMENDED"
    assert d["property_strength_checks"] == "AFTER_PROOF"
    none = plan_strategy(RING, "ring", "prove", [])
    assert decisions(none)["assumption_necessity"] == "NOT_APPLICABLE"
    assert decisions(none)["assertion_generation"] == "RECOMMENDED"
    print("OK\n")

    print("=== 7: request/response signals make progress checking relevant ===")
    arb = (ROOT / "examples" / "arbiter4.v").read_text(encoding="utf-8")
    pp = plan_strategy(arb, "arbiter4", "progress", [])
    pq = plan_strategy(arb, "arbiter4", "prove", [])
    print(f"  progress goal -> {decisions(pp)['progress_check']}   prove goal -> {decisions(pq)['progress_check']}")
    assert decisions(pp)["progress_check"] == "RECOMMENDED"
    assert decisions(pq)["progress_check"] == "OPTIONAL"
    assert "protocol" in " ".join(pp.by_technique("progress_check").caveats)
    print("OK\n")

    print("=== 8: structure of every plan ===")
    for goal in GOALS:
        pl = plan_strategy(RING, "ring", goal, ONEHOT)
        assert pl.steps[0].technique == "plain_proof"
        for s in pl.steps:
            assert s.decision in VOCAB and s.reason.strip(), (goal, s.technique, s.decision)
        assert plan_strategy(RING, "ring", goal, ONEHOT).view() == pl.view(), "planning must be deterministic"
    mesi = (ROOT / "examples" / "mesi_multi_cache.v").read_text(encoding="utf-8")
    pm = plan_strategy(mesi, "", "prove", [])
    assert pm.facts["size_class"] == "UNKNOWN", "state the scan cannot size must not be called small"
    assert "could not be sized" in pm.steps[0].reason
    cache = (ROOT / "examples" / "direct_cache.v").read_text(encoding="utf-8")
    assert plan_strategy(cache, "", "prove", []).facts["size_class"] == "SMALL"
    try:
        plan_strategy(RING, "ring", "nonsense")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown goal must be refused")
    print("OK\n")


if __name__ == "__main__":
    main()
    print("All requested parts passed.")
