"""Signal-wise assertion search (SANGAM-style MCTSr with a solver-grounded
reward). Parts 1-2 are static; Part 3 (--solver) uses real SymbiYosys and a
scripted refiner so the search, the reward and the combination are
deterministic; Part 4 (--llm) is an optional live smoke test with the real
model. Run Parts 3-4 via PowerShell, not Bash (see project memory).
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.assertion_search import (  # noqa: E402
    Assertion, NodeEvaluation, SolverEvaluator, normalize, search_signal, unknown_identifiers,
)
from rtl_verify.dut_probe import generate_probed_rtl  # noqa: E402
from rtl_verify.signal_bank import build_signal_bank  # noqa: E402

RING = """
module ring(input clk, input rst_n, input adv, output [3:0] q);
    reg [3:0] st;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) st <= 4'b0001;
        else if (adv) st <= {st[2:0], st[3]};
    assign q = st;
endmodule
"""

SPEC = ("The output q is a one-hot ring. When adv is high, q rotates left by one position. "
        "When adv is low, q holds its value. The signal ghost_sig described elsewhere is not part of this block.")


def _probed():
    mod = analyze_rtl(RING, top_module="ring")
    psrc, pmod, _ = generate_probed_rtl(RING, mod)
    return psrc, pmod


def part1_static() -> None:
    print("=== Part 1: signal bank (deterministic signal mapping) ===")
    _psrc, pmod = _probed()
    bank = build_signal_bank(pmod, RING, SPEC)
    names = set(bank.signals)
    print(f"  mapped={sorted(names)}  rtl_only={sorted(bank.rtl_only)}")
    assert names == {"q", "adv"}, names                      # present in BOTH spec and RTL
    assert "st" in bank.rtl_only, "a register the spec never mentions must not be mapped"
    assert "ghost_sig" not in names, "a name only the spec has must never be invented into the bank"
    q = bank.signals["q"]
    assert q.expr_name == "q" and q.width == 4 and q.kind == "output"
    assert any("assign q = st" in ln for ln in q.driver_lines), q.driver_lines
    assert "adv" in q.related
    assert any("one-hot" in s for s in q.spec_sentences)
    nobank = build_signal_bank(pmod, RING, "")
    assert {"q", "adv", "st"} <= set(nobank.signals) and nobank.signals["st"].expr_name == "__dbg_st"
    print("OK\n")

    print("=== Part 2: expression hygiene and the UCT / back-propagation arithmetic ===")
    known = {"q", "adv", "__dbg_st"}
    assert unknown_identifiers("$onehot(q) && (q != 4'b0000)", known) == []
    assert unknown_identifiers("!$past(adv) || (q == $past(q))", known) == []
    assert unknown_identifiers("ghost == 1'b1 && q[0]", known) == ["ghost"]
    assert normalize("( a && b )") == "a&&b" and normalize("(a)&&(b)") == "(a)&&(b)"

    class FakeEvaluator:
        target_count = 4

        def evaluate(self, assertions):
            return NodeEvaluation([], float(assertions[0].expr[1:]), "fb")

        def judge(self, a):
            from rtl_verify.assertion_search import Verdict
            return Verdict("UNKNOWN")

        def prune_redundant(self, items):
            return items, []

    class ScriptedRewards:
        def __init__(self, rewards):
            self.r = list(rewards)

        def initial(self, info):
            return [Assertion("a", "r10")]

        def refine(self, info, assertions, feedback, rollout):
            return [Assertion("a", f"r{self.r.pop(0)}")]

    from rtl_verify.signal_bank import SignalInfo
    res = search_signal(SignalInfo("s", "s", "output", 1), FakeEvaluator(), ScriptedRewards([50, 20]),
                        rollouts=2, c=1.4, max_children=2)
    q = {n.id: round(n.q, 3) for n in res.nodes}
    par = {n.id: n.parent for n in res.nodes}
    print(f"  parents={par}  q={q}")
    # rollout 1: root(10) -> child 50; root.q = (10+50)/2 = 30.
    # rollout 2: UCT favours the high-reward child over the root; its child 20
    #   gives child.q = (50+20)/2 = 35 and root.q = (30 + 35)/2 = 32.5.
    assert par == {0: None, 1: 0, 2: 1}, par
    assert q == {0: 32.5, 1: 35.0, 2: 20.0}, q
    print("OK\n")


class Script:
    """Refiner that returns pre-written assertion sets, in order."""

    def __init__(self, initial, rounds):
        self._initial, self._rounds = initial, list(rounds)

    def initial(self, info):
        return [dataclasses.replace(a) for a in self._initial]

    def refine(self, info, assertions, feedback, rollout):
        assert feedback.strip(), "the refiner must receive solver feedback"
        return [dataclasses.replace(a) for a in self._rounds.pop(0)]


def part3_solver() -> None:
    from rtl_verify.backends.symbiyosys import SymbiYosysBackend

    psrc, pmod = _probed()
    work = Path(tempfile.mkdtemp(prefix="asrch_test_"))
    rtl = work / "dut.v"
    rtl.write_text(psrc, encoding="utf-8")
    bank = build_signal_bank(pmod, RING, SPEC)
    info = bank.signals["q"]

    ONEHOT = Assertion("onehot", "$onehot(q)", "width")
    HOLD = Assertion("hold", "!(!$past(adv)) || (q == $past(q))", "function")
    NZ = Assertion("nz", "q != 4'b0000", "width")
    BOGUS = Assertion("bogus", "ghost == 1'b1", "function")
    WRONG = Assertion("wrong", "q == 4'b0001", "function")
    VAC = Assertion("vac", "!(q == 4'b0000) || (q == 4'b0001)", "function")

    refiner = Script([ONEHOT], [
        [ONEHOT, NZ, BOGUS, WRONG, VAC],
        [ONEHOT, HOLD],
        [ONEHOT, HOLD, NZ],
        [ONEHOT, HOLD],
    ])
    ev = SolverEvaluator(pmod, rtl, SymbiYosysBackend(), work / "ev", timeout_sec=60)
    res = search_signal(info, ev, refiner, rollouts=4)

    print("=== Part 3a: every assertion is classified by a real run ===")
    node1 = next(n for n in res.nodes if {a.name for a in n.assertions} >= {"bogus", "wrong"})
    status = {a.name: v.status for a, v in node1.evaluation.verdicts}
    print(f"  {status}")
    assert status == {"onehot": "PROVEN_UNCHECKED", "nz": "REDUNDANT", "bogus": "SYNTAX_ERROR",
                      "wrong": "FALSIFIED", "vac": "VACUOUS"}, status
    assert abs(node1.reward - (70 - 10 - 100 - 30 - 20) / 5) < 1e-9, node1.reward
    print("OK\n")

    print("=== Part 3b: reward is deterministic and orders nodes by evidence ===")
    best = res.nodes[res.best_node]
    print(f"  rewards={[round(n.reward, 1) for n in res.nodes]}  best={res.best_node}")
    assert round(best.reward, 6) == 42.5, best.reward
    assert node1.reward < res.nodes[0].reward < best.reward
    print("OK\n")

    print("=== Part 3c: combination keeps proven, irredundant assertions; falsified ones go to review ===")
    kept = sorted(a.name for a, _ in res.kept)
    review = {a.name: v for a, v in res.needs_review}
    dropped = {a.name: v.status for a, v in res.dropped}
    print(f"  kept={kept}  needs_review={sorted(review)}  dropped={dropped}")
    assert kept == ["hold", "onehot"], kept
    assert set(review) == {"wrong"} and review["wrong"].counterexample and "q" in review["wrong"].counterexample
    assert dropped == {"bogus": "SYNTAX_ERROR", "vac": "VACUOUS", "nz": "REDUNDANT"}, dropped
    print(f"  solver runs: {ev.solver_runs}; refiner calls: {res.refiner_calls}")
    print("OK\n")


def part4_llm() -> None:
    from rtl_verify.assertion_search import LLMRefiner
    from rtl_verify.backends.symbiyosys import SymbiYosysBackend

    psrc, pmod = _probed()
    work = Path(tempfile.mkdtemp(prefix="asrch_llm_"))
    rtl = work / "dut.v"
    rtl.write_text(psrc, encoding="utf-8")
    info = build_signal_bank(pmod, RING, SPEC).signals["q"]
    ev = SolverEvaluator(pmod, rtl, SymbiYosysBackend(), work / "ev", timeout_sec=60)
    res = search_signal(info, ev, LLMRefiner(pmod), rollouts=2)
    v = res.view()
    print("=== Part 4: live model ===")
    print(f"  refiner calls={res.refiner_calls} solver runs={ev.solver_runs} best reward="
          f"{res.nodes[res.best_node].reward:.1f}")
    for k in v["kept"]:
        print(f"  KEPT   [{k['category']}] {k['expr']}")
    for k in v["needs_review"]:
        print(f"  REVIEW [{k['category']}] {k['expr']}")
    for k in v["dropped"]:
        print(f"  DROP   {k['status']:12} {k['expr']}")
    assert all(k["status"].startswith("PROVEN") for k in v["kept"])
    known = {p.name for p in pmod.ports}
    assert all(not unknown_identifiers(k["expr"], known) for k in v["kept"])
    print("OK\n")


if __name__ == "__main__":
    part1_static()
    if "--solver" in sys.argv:
        part3_solver()
    if "--llm" in sys.argv:
        part4_llm()
    print("All requested parts passed.")
