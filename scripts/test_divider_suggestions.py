"""Stage 5a of the complexity roadmap: complex math (sequential divider).

divider8.v is genuinely iterative (8 restoring-division steps per divide,
confirmed functionally correct against a real Icarus Verilog simulation
of 10 cases including div-by-zero, before trusting formal results against
it). Its real correctness claim -- quotient*divisor + remainder ==
dividend -- is nonlinear arithmetic tracked across many cycles, not
checkable in one clock edge.

This is expected to surface an honest, slightly ironic finding rather
than a clean "stress test passed": property_to_sva.py's same-cycle-only
converter should DECLINE that correctness claim as multi-cycle (correctly
so, per its own documented contract) -- which means the formal engines
never actually get challenged with the one property that would have been
hard. The same-cycle properties that DO get checked (busy/done mutual
exclusion, div_by_zero implies zero outputs) are simple control claims a
small state space should prove quickly. Worth confirming empirically
rather than assuming either way.

Real LLM calls, real SymbiYosys runs -- not mocked.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.backends.symbiyosys import SymbiYosysBackend  # noqa: E402
from rtl_verify.formal_props import generate_formal_wrapper, recommended_engine_chain  # noqa: E402
from rtl_verify.property_suggester import suggest_properties  # noqa: E402
from rtl_verify.property_to_sva import convert_to_sva  # noqa: E402

GROUND_TRUTH = """
Known-correct properties for this 8-bit restoring divider (for manual
comparison against the suggestions below):

  SAME-CYCLE (should be proposed AND expressible):
    1. busy and done are never both high at once (mutual exclusion)
    2. if div_by_zero is high, quotient==0 and remainder==0

  MULTI-CYCLE (the actually interesting correctness claims -- should be
  proposed, but honestly DECLINED at conversion, since checking them
  requires relating dividend/divisor at start time to quotient/remainder
  many cycles later at done time):
    3. quotient*divisor + remainder == dividend, checked when done fires
       after the start that produced it
    4. remainder < divisor when done (no remainder overflow)
    5. division completes within a bounded number of cycles after start

  REACHABILITY (cover):
    6. done is reachable
    7. div_by_zero is reachable
"""


def main() -> None:
    rtl_path = ROOT / "examples" / "divider8.v"
    rtl_source = rtl_path.read_text(encoding="utf-8")
    module = analyze_rtl(rtl_source, top_module="divider8")

    print(GROUND_TRUTH)
    print("=== Suggesting properties (LLM call 1) ===")
    proposals = suggest_properties(module, rtl_source, spec_text="")
    print(f"Got {len(proposals)} proposals:\n")
    for p in proposals:
        print(f"[{p['kind']:6s}] ({p['pattern']}) {p['description']}")
        print(f"         signals={p['signals']} paired_cover={p.get('paired_cover')!r}")
    assert 1 <= len(proposals) <= 15, f"unexpected proposal count: {len(proposals)}"

    print("\n=== Converting each to SVA (LLM call 2) and running through the Stage 3 engine chain ===\n")
    backend = SymbiYosysBackend()
    n_expressible = 0
    n_declined = 0
    n_proven = 0
    n_falsified = 0
    n_inconclusive = 0

    for p in proposals:
        conv = convert_to_sva(module, p["kind"], p["description"], p.get("rationale", ""))
        if not conv["expressible"]:
            n_declined += 1
            print(f"[{p['kind']}] {p['description']!r} -> NOT EXPRESSIBLE: {conv['note'][:220]}\n")
            continue
        n_expressible += 1
        expr = conv["expr"]
        print(f"[{p['kind']}] {p['description']!r} -> {expr!r}")

        wrapper_sv = generate_formal_wrapper(module, [(p["name"], expr, p["kind"])])
        chain = recommended_engine_chain(module, kind=p["kind"])

        result = None
        attempts = []
        for i, config in enumerate(chain):
            work = Path(tempfile.mkdtemp(prefix=f"divider_e2e_{p['name']}_engine{i}_"))
            wrapper_path = work / "wrapper.sv"
            wrapper_path.write_text(wrapper_sv, encoding="utf-8")
            result = backend.run(
                rtl_path, wrapper_path, work,
                top="divider8_formal_top",
                depth=config["depth"], mode=config["mode"], engine=config["engine"],
                timeout_sec=120,
            )
            attempts.append(f"{config['label']}={result.status}")
            if result.status in ("PASS", "FAIL"):
                break

        if result.status == "PASS":
            n_proven += 1
            verdict = "REACHED" if p["kind"] == "cover" else "PROVEN"
        elif result.status == "FAIL":
            n_falsified += 1
            verdict = "UNREACHED" if p["kind"] == "cover" else "FALSIFIED"
        else:
            n_inconclusive += 1
            verdict = result.status
        print(f"    -> chain [{' -> '.join(attempts)}] -> {verdict}\n")

    print("=== Summary ===")
    print(f"proposed={len(proposals)} expressible={n_expressible} declined={n_declined}")
    print(f"proven/reached={n_proven} falsified/unreached={n_falsified} inconclusive={n_inconclusive}")
    print(
        "\nManually compare against the 7 ground-truth items above. If the multi-cycle "
        "correctness claim (#3) got declined rather than checked, that's expected and "
        "correct per property_to_sva.py's contract -- see scripts/test_multiplier_stress.py "
        "for a direct, hand-written stress test of solver difficulty instead."
    )


if __name__ == "__main__":
    main()
