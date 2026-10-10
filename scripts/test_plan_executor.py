"""Plan execution: one technique at a time, only on what is still open, with
every outcome labelled by what produced it.

Part 1 uses a scripted stand-in for the formal check, so the control flow is
tested exactly and quickly. Part 2 (--solver) runs the real endpoint and must
be run via PowerShell, not Bash (see project memory on yosys spawning).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from rtl_verify.plan_executor import execute_plan  # noqa: E402
from rtl_verify.strategy_planner import plan_strategy  # noqa: E402
import test_cutpoint_and_decompose as tcd  # noqa: E402
import test_param_reduce_and_rom as tpr  # noqa: E402

BIG = tcd._wrapper_with_accumulator()
TOP = "big_soc_wrapper"
PROPS = [
    {"name": "A1", "expr": "1'b1", "kind": "assume"},
    {"name": "p1", "expr": "(grant == 4'd0) || $onehot(grant)", "kind": "assert"},
    {"name": "p2", "expr": "!(grant[0] && grant[1])", "kind": "assert"},
]
ALLOWED = {"rtl_file", "rtl_text", "top_module", "properties", "timeout_sec", "depth_override", "cross_check",
           "rom_to_case", "decompose", "auto_invariants", "counter_abstraction", "blackbox_modules",
           "cut_signals", "data_signals", "data_width_reduction", "param_overrides"}


class Fake:
    """Scripted formal check: `rule(args, n_call) -> {name: verdict}`."""

    def __init__(self, rule):
        self.rule, self.calls = rule, []

    async def __call__(self, **args):
        self.calls.append(args)
        sent = [p for p in json.loads(args["properties"]) if p["kind"] != "assume"]
        verdicts = self.rule(args, len(self.calls))
        if isinstance(verdicts, Exception):
            raise verdicts
        return {"properties": [{"name": p["name"], "verdict": verdicts.get(p["name"], "UNKNOWN")} for p in sent]}


def names_sent(call):
    return [p["name"] for p in json.loads(call["properties"])]


def run(plan, fake, props=PROPS, **kw):
    base = {"rtl_text": "", "top_module": TOP, "timeout_sec": 10}
    return asyncio.run(execute_plan(plan, fake, base, props, ALLOWED, **kw))


def part1_control_flow() -> None:
    plan = plan_strategy(BIG, TOP, "prove", PROPS)
    techs = [s.technique for s in plan.steps if s.decision == "IF_STUCK"]
    print(f"=== plan under test: IF_STUCK = {techs} ===\n")

    print("=== 1: a baseline that settles everything runs nothing else ===")
    f = Fake(lambda a, n: {"p1": "PROVEN", "p2": "PROVEN"})
    rep = run(plan, f)
    assert len(f.calls) == 1 and rep.stopped_because.startswith("every property was settled by the baseline")
    assert all(o.final == "PROVEN" and o.resolved_by.startswith("plain_proof") for o in rep.outcomes.values())
    assert not any(k in f.calls[0] for k in ("blackbox_modules", "cut_signals", "auto_invariants"))
    print("OK\n")

    print("=== 2: later techniques see only the open properties, in plan order ===")
    def rule2(a, n):
        if n == 1:
            return {"p1": "TIMEOUT", "p2": "PROVEN"}
        if a.get("blackbox_modules"):
            return {"p1": "PROVEN"}
        return {"p1": "TIMEOUT"}
    f = Fake(rule2)
    rep = run(plan, f)
    order = [a.technique for a in rep.attempts]
    print(f"  attempts: {order}")
    assert order[0].startswith("plain_proof") and order[-1] == "black_boxing", order
    assert order.index("helper_invariants") < order.index("black_boxing")
    assert "cut_points" not in order, "must stop once the property is settled"
    for c in f.calls[1:]:
        assert names_sent(c) == ["A1", "p1"], names_sent(c)          # assumption kept, p2 not re-run
    assert rep.outcomes["p2"].resolved_by.startswith("plain_proof")
    assert rep.outcomes["p1"].final == "PROVEN" and rep.outcomes["p1"].resolved_by == "black_boxing"
    assert "adds behaviours" in rep.outcomes["p1"].meaning or "only adds" in rep.outcomes["p1"].meaning
    print("OK\n")

    print("=== 3: a FALSIFIED under an abstraction is unconfirmed, and ends that property's search ===")
    def rule3(a, n):
        if n == 1:
            return {"p1": "TIMEOUT", "p2": "PROVEN"}
        return {"p1": "FALSIFIED"} if a.get("blackbox_modules") else {"p1": "TIMEOUT"}
    f = Fake(rule3)
    rep = run(plan, f)
    o = rep.outcomes["p1"]
    print(f"  {o.final}: {o.meaning}")
    assert o.final == "FALSIFIED_UNCONFIRMED" and o.raw_verdict == "FALSIFIED"
    assert "before calling it a bug" in o.meaning
    assert [a.technique for a in rep.attempts][-1] == "black_boxing"
    print("OK\n")

    print("=== 4: invariants are exact, so their FALSIFIED is a real one ===")
    def rule4(a, n):
        if n == 1:
            return {"p1": "TIMEOUT", "p2": "PROVEN"}
        return {"p1": "FALSIFIED"} if a.get("auto_invariants") else {"p1": "TIMEOUT"}
    rep = run(plan, Fake(rule4))
    assert rep.outcomes["p1"].final == "FALSIFIED", rep.outcomes["p1"].final
    print("OK\n")

    print("=== 5: what the goal forbids is skipped, and allowed under bug hunting ===")
    f = Fake(lambda a, n: {"p1": "TIMEOUT", "p2": "TIMEOUT"})
    rep = run(plan, f)
    forbidden = [s for s in rep.skipped if s["technique"] == "parameter_reduction"]
    assert forbidden and "not valid for this goal" in forbidden[0]["why"]
    assert not any("param_overrides" in c for c in f.calls)
    assert all(o.final == "INCONCLUSIVE" for o in rep.outcomes.values())
    assert "exhausted" in rep.stopped_because
    bug_plan = plan_strategy(BIG, TOP, "find_bugs", PROPS)
    def rule5(a, n):
        return {"p1": "PROVEN", "p2": "PROVEN"} if a.get("param_overrides") else {"p1": "TIMEOUT", "p2": "TIMEOUT"}
    rep = run(bug_plan, Fake(rule5))
    assert rep.outcomes["p1"].final == "PROVEN_FOR_REDUCED_CONFIG", rep.outcomes["p1"].final
    assert "not a proof of the design" in rep.outcomes["p1"].meaning
    print("OK\n")

    print("=== 6: attempt limit, failed attempts and ERROR verdicts ===")
    f = Fake(lambda a, n: {"p1": "TIMEOUT", "p2": "TIMEOUT"})
    rep = run(plan, f, max_attempts=2)
    assert len(rep.attempts) == 2 and "attempt limit" in rep.stopped_because
    def rule6(a, n):
        return RuntimeError("solver crashed") if n == 2 else {"p1": "TIMEOUT", "p2": "TIMEOUT"}
    rep = run(plan, Fake(rule6))
    assert "failed to run" in rep.attempts[1].note and len(rep.attempts) > 2, "a failed attempt must not stop the plan"
    rep = run(plan, Fake(lambda a, n: {"p1": "ERROR", "p2": "PROVEN"}))
    assert rep.outcomes["p1"].final == "ERROR" and len(rep.attempts) == 1, "ERROR is a fix, not a reason to escalate"
    print("OK\n")

    print("=== 7: exact transforms join the baseline; no properties means nothing runs ===")
    rom_props = [{"name": "r", "expr": "data == data", "kind": "assert"}]
    rplan = plan_strategy(tpr.ROM_OK, "rom_lookup", "prove", rom_props)
    f = Fake(lambda a, n: {"r": "PROVEN"})
    rep = run(rplan, f, props=rom_props)
    assert f.calls[0].get("rom_to_case") is True and rep.attempts[0].technique == "plain_proof + rom_to_case"
    f = Fake(lambda a, n: {})
    rep = run(plan, f, props=[PROPS[0]])
    assert not f.calls and "nothing to execute" in rep.summary
    print("OK\n")


class _Replay:
    def __init__(self, status):
        self.status, self.violated_at_step = status, 3


def part1b_proactive() -> None:
    plan = plan_strategy(BIG, TOP, "prove", PROPS)
    assert plan.by_technique("black_boxing").decision == "RECOMMENDED", "needs a large design for this test"

    def runp(fake, status=None, **kw):
        async def replay(name, expr, wf):
            replay.calls.append(name)
            return _Replay(status)
        replay.calls = []
        base = {"rtl_text": "", "top_module": TOP, "timeout_sec": 10}
        rep = asyncio.run(execute_plan(plan, fake, base, PROPS, ALLOWED, proactive=True,
                                       replay=None if status is None else replay, **kw))
        return rep, replay

    print("=== 8: proactive: a sound abstraction first; its PROVEN is final ===")
    f = Fake(lambda a, n: {"p1": "PROVEN", "p2": "PROVEN"})
    rep, _ = runp(f)
    assert len(f.calls) == 1 and f.calls[0].get("blackbox_modules"), "the abstraction must run first"
    assert "black_boxing" in rep.attempts[0].technique
    assert all(o.final == "PROVEN" for o in rep.outcomes.values())
    print("OK\n")

    print("=== 9: a FALSIFIED is confirmed by replay on the real design, or sent back to the plain proof ===")
    def rule(a, n):
        return {"p1": "FALSIFIED", "p2": "PROVEN"} if a.get("blackbox_modules") else {"p1": "PROVEN"}
    f = Fake(rule)
    rep, rp = runp(f, "CONFIRMED")
    o = rep.outcomes["p1"]
    assert o.final == "FALSIFIED" and "reproduced on the real design" in o.meaning and len(f.calls) == 1
    assert rp.calls == ["p1"] and "replay on the real design: CONFIRMED" in o.trail[-1]

    f = Fake(rule)
    rep, rp = runp(f, "NOT_REPRODUCED")
    assert len(f.calls) == 2 and not f.calls[1].get("blackbox_modules"), "fallback must be the real design"
    assert names_sent(f.calls[1]) == ["A1", "p1"], names_sent(f.calls[1])
    assert rep.outcomes["p1"].final == "PROVEN" and rep.outcomes["p1"].resolved_by.startswith("plain_proof")
    assert rep.outcomes["p2"].resolved_by.endswith("black_boxing")

    f = Fake(rule)
    rep, _ = runp(f, None)            # no replay available
    assert len(f.calls) == 2 and rep.outcomes["p1"].final == "PROVEN"
    assert "falling back" in " ".join(rep.outcomes["p1"].trail)
    print("OK\n")

    print("=== 10: an inconclusive abstraction falls back, and is not retried later ===")
    f = Fake(lambda a, n: {"p1": "TIMEOUT", "p2": "TIMEOUT"})
    rep, _ = runp(f, "NOT_REPRODUCED")
    techs = [a.technique for a in rep.attempts]
    assert techs[0].endswith("black_boxing") and techs[1].endswith("(real design)"), techs
    assert not any(t == "black_boxing" for t in techs[2:]), techs
    assert all(o.final == "INCONCLUSIVE" for o in rep.outcomes.values())
    print("OK\n")


def part2_solver() -> None:
    import api.main as m

    def call(src, top, props, **kw):
        return asyncio.run(m.formal_plan_execute(
            rtl_file=None, rtl_text=src, top_module=top, goal="prove", properties=json.dumps(props),
            data_signals="", timeout_sec=kw.get("timeout_sec", 60), depth_override=0, cross_check=True,
            max_attempts=kw.get("max_attempts", 6)))

    print("=== Part 2a: a design that needs nothing gets exactly the plain proof ===")
    ring = ("module ring(input clk, input rst_n, input adv, output [3:0] q);\n"
            "  reg [3:0] st;\n"
            "  always @(posedge clk or negedge rst_n) if (!rst_n) st <= 4'b0001; else if (adv) st <= {st[2:0], st[3]};\n"
            "  assign q = st;\nendmodule\n")
    out = call(ring, "ring", [{"name": "onehot", "expr": "$onehot(q)", "kind": "assert"}])
    print(f"  {out['summary']}")
    assert len(out["attempts"]) == 1 and out["outcomes"][0]["final"] == "PROVEN"
    assert out["attempts"][0]["technique"] == "plain_proof"
    print("OK\n")

    print("=== Part 2b: a stuck design is escalated and the result says what produced it ===")
    out = call(BIG, TOP, PROPS[1:2] + [PROPS[0]], timeout_sec=40, max_attempts=4)
    o = out["outcomes"][0]
    print(f"  {out['summary']}")
    for a in out["attempts"]:
        print(f"    {a['technique']:22} -> {a['verdicts']}")
    assert o["final"] in ("PROVEN", "FALSIFIED_UNCONFIRMED", "INCONCLUSIVE"), o["final"]
    assert o["final"] == "PROVEN", o
    print("OK\n")


if __name__ == "__main__":
    part1_control_flow()
    part1b_proactive()
    if "--solver" in sys.argv:
        part2_solver()
    print("All requested parts passed.")
