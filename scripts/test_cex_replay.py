"""Counterexample replay (src/rtl_verify/cex_replay.py). Needs Icarus Verilog
and SymbiYosys; run via PowerShell, not Bash.

What is pinned:
  - a real counterexample is CONFIRMED when its inputs are replayed on the
    design, including a property that uses $past;
  - the same trace against a property that is true is NOT_REPRODUCED;
  - a counter-abstraction counterexample is NOT_REPRODUCED on the real design
    (its trace is short because the counter was allowed to jump), which is
    exactly why NOT_REPRODUCED must send the caller to a real proof and is
    never read as "the counterexample was spurious".
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))

import api.main as app  # noqa: E402
from rtl_verify.cex_replay import _split_past, replay_counterexample  # noqa: E402
from families import counter_thr  # noqa: E402

RING = """
module ring(input clk, input rst_n, input adv, output [3:0] q);
    reg [3:0] st;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) st <= 4'b0001;
        else if (adv) st <= {st[2:0], st[3]};
    assign q = st;
endmodule
"""
DEFAULTS = {n: getattr(p.default, "default", p.default)
            for n, p in inspect.signature(app.formal_check).parameters.items()}


def formal(src, top, expr, **extra):
    out = asyncio.run(app.formal_check(**{
        **DEFAULTS, "rtl_file": None, "rtl_text": src, "top_module": top,
        "properties": json.dumps([{"name": "p", "expr": expr, "kind": "assert"}]),
        "timeout_sec": 60, "cross_check": False, **extra}))
    r = out["properties"][0]
    return r["verdict"], r.get("waveform_json")


def main() -> None:
    print("=== 1: $past is rewritten, and unsupported forms are refused ===")
    new, inners = _split_past("$past(adv) || (q == $past(q[1:0]))")
    assert new == "__past_0 || (q == __past_1)" and inners == ["adv", "q[1:0]"], (new, inners)
    try:
        _split_past("$past(x, 3)")
    except ValueError:
        pass
    else:
        raise AssertionError("a depth argument must be refused, not silently approximated")
    print("OK\n")

    print("=== 2: a real counterexample is confirmed; the same trace does not break a true property ===")
    src, mod = app._analyze_and_probe(RING, "ring")
    for expr in ("q != 4'd4", "$past(adv) || (q != $past(q))"):
        verdict, wf = formal(RING, "ring", expr)
        assert verdict == "FALSIFIED" and wf, (expr, verdict)
        res = replay_counterexample(mod, src, expr, wf)
        other = replay_counterexample(mod, src, "q != 4'd0", wf)
        print(f"  {expr:<36} replay={res.status} at step {res.violated_at_step}; true property: {other.status}")
        assert res.status == "CONFIRMED" and other.status == "NOT_REPRODUCED"
    print("OK\n")

    print("=== 3: a counter-abstraction counterexample does not reproduce on the real counter ===")
    inst = counter_thr(3000)
    verdict, wf = formal(inst.rtl, inst.top, "!timed_out", counter_abstraction="cnt")
    psrc, pmod = app._analyze_and_probe(inst.rtl, inst.top)
    res = replay_counterexample(pmod, psrc, "!timed_out", wf)
    print(f"  abstraction verdict={verdict}; replay on the real design: {res.status} ({res.steps} steps)")
    assert verdict == "FALSIFIED"
    assert res.status == "NOT_REPRODUCED", "the real counter cannot have reached 3000 in this short trace"
    print("OK\n")

    print("=== 4: no trace or no simulator is UNAVAILABLE, not a guess ===")
    assert replay_counterexample(mod, src, "q != 4'd4", None).status == "UNAVAILABLE"
    assert replay_counterexample(mod, src, "q != 4'd4", {"signals": []}).status == "UNAVAILABLE"
    print("OK\n")


if __name__ == "__main__":
    main()
    print("All requested parts passed.")
