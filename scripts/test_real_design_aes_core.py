"""Real end-to-end exercise of this session's newer formal-methodology
features -- auto black-box candidate ranking, assumption consistency,
signal coverage, and mutation adequacy -- against a genuinely different
class of real design from everything tested so far: secworks/aes, a
real, widely-used open-source AES-128/256 core. Unlike the CPUs,
Ethernet MACs, and FIFOs tested earlier this session, this is arithmetic
(XOR-network) and lookup-table (S-box) heavy, and its top-level control
module (aes_core.v) instantiates the FULL encryption/decryption/key-
schedule datapath even for a property that's purely about control-FSM
correctness -- exactly the "real complexity pulled into a property's
cone of influence despite no semantic dependency" shape this project's
black-boxing feature exists for.

This is also where a real, honest gap in mutate.py was found and fixed:
aes_core.v's control FSM is built entirely from `case` statements and
named constants (CTRL_IDLE/CTRL_INIT/CTRL_NEXT), with NO relational,
logical, bitwise, or arithmetic operators anywhere in it -- confirmed
directly by grepping the real file, not assumed -- so the original
operator-swap-only mutation families found literally nothing to mutate
despite the module having real, mutable control state. Fixed by adding
a "constant" mutation family (sized-literal increment) to mutate.py.

Must be run via PowerShell, not the Bash tool, per this project's own
persistent memory note on yosys subprocess spawning.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.blackbox import recommend_blackbox_candidates  # noqa: E402
from rtl_verify.mutate import generate_mutants  # noqa: E402


def main() -> None:
    aes_path = ROOT.parent / "external_rtl_cache" / "aes_combined.v"
    if not aes_path.is_file():
        print(
            "(skipping: fetch aes.v, aes_core.v, aes_encipher_block.v, "
            "aes_decipher_block.v, aes_key_mem.v, aes_sbox.v, aes_inv_sbox.v "
            "from github.com/secworks/aes (src/rtl/), concatenate them, and "
            f"save the result to {aes_path} to include this real-design check)"
        )
        return

    source = aes_path.read_text(encoding="utf-8")

    print("=== Real: aes_core.v instantiates the FULL encryption/decryption/"
          "key-schedule datapath -- a good black-boxing candidate set for a "
          "control-only property ===")
    mod = analyze_rtl(source, top_module="aes_core")
    candidates = recommend_blackbox_candidates(mod, source)
    print(f"  candidates: {[(c.module_name, c.reason) for c in candidates]}")
    assert {"aes_encipher_block", "aes_decipher_block", "aes_key_mem"} <= set(
        c.module_name for c in candidates
    ), candidates
    print("OK\n")

    print("=== Real gap found and fixed: aes_core's own control FSM (case "
          "statements + named constants, no operators at all) needed the new "
          "'constant' mutation family to have anything mutable ===")
    mutants = generate_mutants(mod, source, max_mutants=20)
    print(f"  {len(mutants)} mutants generated for aes_core's own body")
    assert mutants, "aes_core's case-statement FSM must now yield real constant mutants"
    assert all(m.operator.startswith("constant:") for m in mutants), (
        "aes_core.v's control logic has no relational/logical/bitwise/arithmetic "
        "operators at all -- every mutant here must come from the constant family",
        mutants,
    )
    print("OK\n")

    from api.main import formal_check  # noqa: E402 (local import: avoids app import cost above)

    print("=== Real: a genuine FSM invariant on aes_core, proven, with "
          "assumption consistency and signal coverage both reporting honestly ===")
    props = json.dumps([
        {
            "name": "idle_implies_ready",
            "expr": "(__dbg_aes_core_ctrl_reg == 2'h0) == ready",
            "kind": "assert",
        },
        {"name": "no_simultaneous_init_next", "expr": "!(init && next)", "kind": "assume"},
    ])
    result = asyncio.run(formal_check(
        rtl_file=None, rtl_text=source, top_module="aes_core",
        properties=props, timeout_sec=90, depth_override=0,
        cross_check=False, blackbox_modules="", auto_blackbox=True,
    ))
    r = result["properties"][0]
    print(f"  verdict: {r['verdict']}")
    print(f"  assumption_consistency: {result['assumption_consistency']['status']}")
    print(f"  signal_coverage: {result['signal_coverage']['coverage_percent']:.0f}% "
          f"({len(result['signal_coverage']['covered_ports'])}/"
          f"{result['signal_coverage']['total_ports']} ports)")
    assert r["verdict"] == "PROVEN", r
    assert result["assumption_consistency"]["status"] == "CONSISTENT", result["assumption_consistency"]
    # A property this narrowly scoped to control logic must leave the real
    # encryption datapath (block/result/key) entirely unreferenced --
    # confirming signal_coverage reports that honestly, not just that
    # SOME coverage number came back.
    assert "block" in result["signal_coverage"]["uncovered_ports"], result["signal_coverage"]
    assert "key" in result["signal_coverage"]["uncovered_ports"], result["signal_coverage"]
    print("OK\n")

    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
