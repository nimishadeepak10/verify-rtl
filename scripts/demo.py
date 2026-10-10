"""Five-minute demo: plan, execute, read the verdicts.

    python scripts/demo.py

Runs on examples/sync_fifo.v with three true properties and one deliberately
false one. No API key is needed. Requires SymbiYosys and Yosys on PATH (on
Windows run it from PowerShell).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import api.main as app  # noqa: E402

PROPERTIES = [
    {"name": "count_within_capacity", "expr": "count <= 3'd4", "kind": "assert"},
    {"name": "full_xor_empty", "expr": "!(full && empty)", "kind": "assert"},
    {"name": "full_matches_count", "expr": "full == (count == 3'd4)", "kind": "assert"},
    {"name": "deliberately_wrong", "expr": "count <= 3'd3", "kind": "assert"},
]


def main() -> None:
    src = (ROOT / "examples" / "sync_fifo.v").read_text(encoding="utf-8")
    common = dict(rtl_file=None, rtl_text=src, top_module="sync_fifo", goal="prove",
                  properties=json.dumps(PROPERTIES), data_signals="")

    plan = asyncio.run(app.formal_plan(has_spec=False, **common))
    print("1. PLAN (no solver run)")
    print(f"   {plan['summary']}\n")
    for step in plan["steps"]:
        if step["decision"] not in ("NOT_APPLICABLE",):
            print(f"   {step['decision']:<24} {step['technique']:<26} {step['reason'][:70]}")
    skipped = [s["technique"] for s in plan["steps"] if s["decision"] == "NOT_APPLICABLE"]
    print(f"\n   not applicable to this design: {', '.join(skipped)}\n")

    print("2. EXECUTE")
    out = asyncio.run(app.formal_plan_execute(timeout_sec=60, depth_override=0, cross_check=True,
                                              max_attempts=4, **{k: v for k, v in common.items()
                                                                 if k != "has_spec"}))
    for o in out["outcomes"]:
        print(f"   {o['name']:<24} {o['final']:<12} {o['meaning']}")
    print(f"\n   {len(out['attempts'])} attempt(s); stopped because: {out['stopped_because']}")
    print("\nThe wrong property is FALSIFIED with a real counterexample; the true ones are PROVEN for all "
          "time, cross-checked by a second engine.")


if __name__ == "__main__":
    main()
