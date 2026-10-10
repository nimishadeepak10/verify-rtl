"""Evaluate technique-selection strategies on the benchmark families.

    python bench/run_bench.py --scale pilot --budget 40 --repeats 1
    python bench/run_bench.py --strategies plain,blind,planner --scale full

Strategies (the only thing that differs between them is how the techniques are
chosen; the solver and the executor are the same):
  plain    the engine chain, no abstraction
  blind    the formal check's own auto_* escalation, every technique enabled
  planner  this project's rule-based planner, run by the plan executor
  proactive the same planner, applying a recommended sound abstraction first and
           confirming its counterexamples by replay on the real design
  llm      a language model chooses the techniques; same executor
  oracle   the parameters a human expert would pass (an upper bound)

Each (instance, strategy, repeat) is scored against known truth:
  correct            PROVEN a true property, or FALSIFIED a false one
  found_unconfirmed  FALSIFIED_UNCONFIRMED a false property (found, flagged)
  reduced_only       PROVEN only for a reduced configuration (not a proof)
  false_alarm        FALSIFIED_UNCONFIRMED a true property (flagged, benign)
  unsound            PROVEN a false property, or FALSIFIED (claimed real) a true one
  unsettled          no definitive result
and costed in wall-clock seconds, formal-check calls and LLM tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))

import api.main as app  # noqa: E402
from rtl_verify import llm_client, plan_executor, regression, strategy_planner  # noqa: E402
from families import Instance, all_instances  # noqa: E402

_DEFAULTS = {n: getattr(p.default, "default", p.default)
             for n, p in inspect.signature(app.formal_check).parameters.items()}
BLIND = {"auto_invariants": True, "auto_blackbox": True, "auto_counter_abstraction": True,
         "auto_cutpoint": True, "auto_data_width_reduction": True, "auto_param_reduction": True}


class Counter:
    """Counts formal-check calls so strategies can be compared on effort."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, **kw):
        self.calls += 1
        return await app.formal_check(**{**_DEFAULTS, **kw})


def score(truth: bool, final: str) -> str:
    if truth:
        return {"PROVEN": "correct", "PROVEN_FOR_REDUCED_CONFIG": "reduced_only",
                "FALSIFIED": "unsound", "FALSIFIED_UNCONFIRMED": "false_alarm"}.get(final, "unsettled")
    return {"FALSIFIED": "correct", "FALSIFIED_UNCONFIRMED": "found_unconfirmed",
            "PROVEN": "unsound", "PROVEN_FOR_REDUCED_CONFIG": "unsound"}.get(final, "unsettled")


def _base(inst: Instance, budget: int) -> dict:
    return {"rtl_file": None, "rtl_text": inst.rtl, "top_module": inst.top, "timeout_sec": budget,
            "depth_override": 0, "cross_check": False}


async def _direct(inst: Instance, budget: int, extra: dict, counter: Counter):
    args = {**_base(inst, budget), "properties": json.dumps(inst.props), **extra}
    out = await counter(**args)
    return {r["name"]: str(r.get("verdict")) for r in out.get("properties", [])}, ["formal_check"]


async def _executed(plan, inst: Instance, budget: int, counter: Counter, order=None, proactive=False):
    replay = None
    if proactive:
        from rtl_verify import cex_replay
        probed_source, probed_mod = app._analyze_and_probe(inst.rtl, inst.top)

        async def replay(name, expr, wf):  # noqa: F811
            return await asyncio.to_thread(cex_replay.replay_counterexample, probed_mod, probed_source, expr, wf)

    rep = await plan_executor.execute_plan(plan, counter, _base(inst, budget), inst.props, set(_DEFAULTS),
                                           max_attempts=6, order=order, proactive=proactive, replay=replay)
    return {n: o.final for n, o in rep.outcomes.items()}, [a.technique for a in rep.attempts]


async def run_one(inst: Instance, strategy: str, budget: int) -> dict:
    counter = Counter()
    before = llm_client.usage_snapshot()
    t0 = time.perf_counter()
    err = None
    try:
        if strategy == "plain":
            finals, trail = await _direct(inst, budget, {}, counter)
        elif strategy == "blind":
            finals, trail = await _direct(inst, budget, BLIND, counter)
        elif strategy == "oracle":
            finals, trail = await _direct(inst, budget, dict(inst.oracle), counter)
        elif strategy == "planner":
            plan = strategy_planner.plan_strategy(inst.rtl, inst.top, "prove", inst.props)
            finals, trail = await _executed(plan, inst, budget, counter)
        elif strategy == "proactive":
            plan = strategy_planner.plan_strategy(inst.rtl, inst.top, "prove", inst.props)
            finals, trail = await _executed(plan, inst, budget, counter, proactive=True)
        elif strategy == "llm":
            import llm_select
            plan, order = llm_select.select(inst.rtl, inst.top, inst.props)
            finals, trail = await _executed(plan, inst, budget, counter, order)
        else:
            raise ValueError(strategy)
    except Exception as e:  # noqa: BLE001 - a crashed run is a result, not a crash of the benchmark
        finals, trail, err = {}, [], f"{type(e).__name__}: {str(e)[:120]}"
    wall = time.perf_counter() - t0
    tokens = llm_client.usage_since(before)
    scored = {p["name"]: score(inst.truth[p["name"]], finals.get(p["name"], "UNKNOWN"))
              for p in inst.props if p.get("kind", "assert") != "assume"}
    return {"instance": inst.id, "family": inst.family, "strategy": strategy, "wall_s": round(wall, 2),
            "calls": counter.calls, "llm_calls": tokens["calls"], "tokens_in": tokens["input_tokens"],
            "tokens_out": tokens["output_tokens"], "finals": finals, "scored": scored, "trail": trail,
            "error": err}


def summarize(rows: list) -> str:
    out = []
    strategies = sorted({r["strategy"] for r in rows})
    cats = ["correct", "found_unconfirmed", "reduced_only", "false_alarm", "unsound", "unsettled"]
    out.append(f"{'strategy':<9}{'props':>6}" + "".join(f"{c[:11]:>13}" for c in cats)
               + f"{'wall s':>9}{'median s':>10}{'calls':>7}{'tokens':>9}")
    for s in strategies:
        rs = [r for r in rows if r["strategy"] == s]
        flat = [v for r in rs for v in r["scored"].values()]
        walls = [r["wall_s"] for r in rs]
        toks = sum(r["tokens_in"] + r["tokens_out"] for r in rs)
        out.append(f"{s:<9}{len(flat):>6}" + "".join(f"{flat.count(c):>13}" for c in cats)
                   + f"{sum(walls):>9.1f}{statistics.median(walls):>10.1f}"
                   + f"{sum(r['calls'] for r in rs):>7}{toks:>9}")
    return "\n".join(out)


def _taskkill(pid: int) -> None:
    import platform
    import subprocess
    if platform.system() == "Windows":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
    else:
        import os
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_isolated(inst_id: str, strategy: str, budget: int, scale: str, wall: int) -> dict:
    """One run in its own process with a HARD wall-clock limit, so a strategy
    that stalls is scored as unsettled instead of stalling the benchmark."""
    import subprocess
    cmd = [sys.executable, str(Path(__file__)), "--child", "--only", inst_id, "--strategies", strategy,
           "--budget", str(budget), "--scale", scale]
    t0 = time.perf_counter()
    kw = {} if sys.platform == "win32" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
    try:
        out, _err = proc.communicate(timeout=wall)
    except subprocess.TimeoutExpired:
        _taskkill(proc.pid)
        try:
            proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        inst = next(i for i in all_instances(scale) if i.id == inst_id)
        return {"instance": inst_id, "family": inst.family, "strategy": strategy,
                "wall_s": round(time.perf_counter() - t0, 2), "calls": 0, "llm_calls": 0,
                "tokens_in": 0, "tokens_out": 0, "finals": {},
                "scored": {p["name"]: "unsettled" for p in inst.props if p.get("kind", "assert") != "assume"},
                "trail": [], "error": f"wall budget of {wall}s exceeded"}
    for line in out.splitlines():
        if line.startswith("ROW:"):
            return json.loads(line[4:])
    raise RuntimeError(f"child produced no row for {inst_id}/{strategy}: {out[-300:]}")


async def child_main(a) -> None:
    # Benchmark runs must not read or write the project's regression baselines:
    # a stale baseline makes the formal check silently re-prove every property
    # ever recorded for a module of the same name.
    import tempfile
    regression.REGRESSION_DIR = Path(tempfile.mkdtemp(prefix="bench_regress_"))
    inst = next(i for i in all_instances(a.scale) if i.id == a.only)
    row = await run_one(inst, a.strategies, a.budget)
    print("ROW:" + json.dumps(row))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", default="pilot", choices=["pilot", "full"])
    ap.add_argument("--strategies", default="plain,blind,planner,oracle")
    ap.add_argument("--budget", type=int, default=40, help="timeout_sec handed to every formal check")
    ap.add_argument("--wall", type=int, default=300, help="hard wall-clock limit per run, seconds")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--only", default="", help="comma-separated instance ids")
    ap.add_argument("--out", default="")
    ap.add_argument("--child", action="store_true")
    a = ap.parse_args()
    if a.child:
        asyncio.run(child_main(a))
        return
    insts = all_instances(a.scale)
    if a.only:
        keep = set(a.only.split(","))
        insts = [i for i in insts if i.id in keep]
    strategies = a.strategies.split(",")
    out_path = Path(a.out) if a.out else ROOT / "bench" / "results" / f"run_{int(time.time())}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with out_path.open("w", encoding="utf-8") as fh:
        for inst in insts:
            for strat in strategies:
                for rep in range(a.repeats):
                    row = run_isolated(inst.id, strat, a.budget, a.scale, a.wall)
                    row["repeat"] = rep
                    rows.append(row)
                    fh.write(json.dumps(row) + chr(10))
                    fh.flush()
                    print(f"{inst.id:<22}{strat:<8}r{rep} {row['wall_s']:>7.1f}s calls={row['calls']} "
                          f"tok={row['tokens_in'] + row['tokens_out']:<6} {row['scored']} {row['trail']}"
                          + (f"  !! {row['error']}" if row.get("error") else ""), flush=True)
    print(chr(10) + summarize(rows))
    print(chr(10) + f"results: {out_path}")


if __name__ == "__main__":
    main()
