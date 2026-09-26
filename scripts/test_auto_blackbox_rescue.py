"""Real, solver-based end-to-end demonstration: a proof that genuinely
times out gets automatically rescued by black-boxing the module
recommend_blackbox_candidates() identifies as the likely cause.

Reuses this project's own black-boxing stress design (examples/arbiter4.v
+ wide_mac_pipeline.v + big_soc_wrapper.v, already used by
scripts/test_blackbox_reduction.py to measure the manual black-boxing
speedup) at a much deeper pipeline (800 stages, vs. the committed
example's 200) so the PLAIN run genuinely exceeds a short timeout budget
within this test -- not just "slower," but a real TIMEOUT, the actual
trigger condition auto_blackbox is designed to react to.

Must be run via PowerShell, not the Bash tool, on this project's
Windows/oss-cad-suite setup -- see this project's own persistent
memory note on yosys subprocess spawning through Bash vs PowerShell.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from api.main import formal_check  # noqa: E402

PROP = [{"name": "grant_onehot0", "expr": "(grant == 4'd0) || $onehot(grant)", "kind": "assert"}]


def main() -> None:
    arbiter = (ROOT / "examples" / "arbiter4.v").read_text(encoding="utf-8")
    mac = (ROOT / "examples" / "wide_mac_pipeline.v").read_text(encoding="utf-8")
    wrapper = (ROOT / "examples" / "big_soc_wrapper.v").read_text(encoding="utf-8")
    # Deepen the pipeline well beyond the committed example's 200 stages --
    # confirmed necessary: at 200 stages the plain proof still finishes in
    # ~13s (see test_blackbox_reduction.py), under the 30s-per-engine-
    # attempt floor _run_chain enforces, so it would never actually
    # produce a TIMEOUT to rescue from.
    wrapper = wrapper.replace("STAGES(200)", "STAGES(800)")
    source = "\n".join([arbiter, mac, wrapper])

    print("=== Real: a genuinely stuck proof (800-stage MAC pipeline gating an "
          "arbiter property) rescued by auto_blackbox ===")
    result = asyncio.run(formal_check(
        rtl_file=None, rtl_text=source, top_module="big_soc_wrapper",
        properties=json.dumps(PROP), timeout_sec=40, depth_override=0,
        cross_check=False, blackbox_modules="", auto_blackbox=True,
    ))
    r = result["properties"][0]
    print(f"  verdict: {r['verdict']}")
    print(f"  attempts: {[(a['label'], a['status']) for a in r['attempts']]}")
    print(f"  auto_blackbox: {json.dumps(r['auto_blackbox'])[:300]}")

    assert r["verdict"] == "PROVEN", (
        "expected the plain run to time out and auto_blackbox to rescue it to PROVEN", r
    )
    ab = r["auto_blackbox"]
    assert ab is not None and ab["resolved"] is True, ab
    assert ab["blackboxed_module"] == "wide_mac_pipeline", ab
    assert any(a["status"] == "TIMEOUT" for a in r["attempts"]), (
        "expected at least one genuine TIMEOUT attempt before the rescue", r["attempts"]
    )
    assert r["cross_check"]["performed"] is False, (
        "cross-check must be skipped, not silently run against a different (non-black-boxed) design", r
    )
    print("OK\n")

    print("=== Sanity: auto_blackbox=False on the same stuck design -> stays inconclusive ===")
    result2 = asyncio.run(formal_check(
        rtl_file=None, rtl_text=source, top_module="big_soc_wrapper",
        properties=json.dumps(PROP), timeout_sec=40, depth_override=0,
        cross_check=False, blackbox_modules="", auto_blackbox=False,
    ))
    r2 = result2["properties"][0]
    print(f"  verdict: {r2['verdict']}")
    assert r2["verdict"] in ("TIMEOUT", "UNKNOWN", "CANCELLED"), (
        "without auto_blackbox, the same stuck design must stay honestly inconclusive, "
        "not silently resolve on its own", r2
    )
    assert r2["auto_blackbox"] is None, r2["auto_blackbox"]
    print("OK\n")

    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
