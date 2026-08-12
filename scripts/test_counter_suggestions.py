"""Stage 4 of the complexity roadmap: counters and control-heavy designs.

updown_counter.v is the next rung after sync_fifo.v (simple pointer
arithmetic) and traffic_light_fsm.v (simple state cycling): it combines
real arithmetic saturation boundaries (no wraparound at 0x00/0xFF) with
real control-flow priority (clear beats load beats enable) and a
registered status FSM whose correctness depends on both.

GROUND_TRUTH below is the same kind of hand-authored comparison set used
in test_fifo_suggestions.py -- for manual review, not auto-graded.

This is also the first test script to run each proposal through Stage 3's
recommended_engine_chain() (PDR -> k-induction/yices -> k-induction/z3)
instead of the single fixed engine test_fifo_suggestions.py used, since
that fallback logic didn't exist yet at Stage 2.

Real LLM calls, real SymbiYosys runs -- not mocked, same as every other
test in this project.
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
Known-correct properties a competent verification engineer would write for
this saturating up/down counter + status FSM (for manual comparison
against what the suggestion engine actually proposes below):

  SAME-CYCLE (should be proposed AND expressible):
    1. saturated == (enable && ((up_down && count==8'hFF) || (!up_down && count==8'h00)))
       -- tautological by construction (it's a direct assign), but still
       worth checking the LLM can express the DUT's own combinational
       definition correctly.
    2. state != 2'd3          -- the 2-bit state reg only uses 3 of 4
       encodings; the 4th is an unreachable bit pattern.
    3. count is representable in 8 bits (trivial by width, low value).

  MULTI-CYCLE (should be proposed, but honestly DECLINED at conversion --
  every one of these is a "this cycle's inputs determine NEXT cycle's
  count" claim, which property_to_sva.py's same-cycle-only converter
  should decline rather than approximate):
    4. clear beats load beats enable (priority ordering, next-cycle)
    5. no wraparound at max: count==0xFF, up_down=1, enable=1, !load,
       !clear => count stays 0xFF next cycle
    6. no wraparound at min: symmetric, count==0x00 case
    7. state==SATURATED correctly lags saturated by exactly one cycle

  REACHABILITY (cover):
    8. count reaches 0xFF is reachable (note: reachable in as few as 2
       cycles via load=1,load_value=0xFE then enable+up_down=1 -- the
       solver isn't limited to "count up from reset one at a time", so
       the existing depth heuristic should be nowhere near a constraint
       here, unlike a hypothetical "reach 0xFF by pure increment" case)
    9. state reaches SATURATED (2'd2) is reachable
"""


def main() -> None:
    rtl_path = ROOT / "examples" / "updown_counter.v"
    rtl_source = rtl_path.read_text(encoding="utf-8")
    module = analyze_rtl(rtl_source, top_module="updown_counter")

    print(GROUND_TRUTH)
    print("=== Suggesting properties (LLM call 1) ===")
    proposals = suggest_properties(module, rtl_source, spec_text="")
    print(f"Got {len(proposals)} proposals:\n")
    for p in proposals:
        print(f"[{p['kind']:6s}] ({p['pattern']}) {p['description']}")
        print(f"         signals={p['signals']} paired_cover={p.get('paired_cover')!r}")
    assert 1 <= len(proposals) <= 15, f"unexpected proposal count: {len(proposals)}"
    assert all(p["kind"] in ("assert", "assume", "cover") for p in proposals)

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
            print(f"[{p['kind']}] {p['description']!r} -> NOT EXPRESSIBLE: {conv['note'][:200]}\n")
            continue
        n_expressible += 1
        expr = conv["expr"]
        print(f"[{p['kind']}] {p['description']!r} -> {expr!r}")

        wrapper_sv = generate_formal_wrapper(module, [(p["name"], expr, p["kind"])])
        chain = recommended_engine_chain(module, kind=p["kind"])

        result = None
        attempts = []
        for i, config in enumerate(chain):
            work = Path(tempfile.mkdtemp(prefix=f"counter_e2e_{p['name']}_engine{i}_"))
            wrapper_path = work / "wrapper.sv"
            wrapper_path.write_text(wrapper_sv, encoding="utf-8")
            result = backend.run(
                rtl_path, wrapper_path, work,
                top="updown_counter_formal_top",
                depth=config["depth"], mode=config["mode"], engine=config["engine"],
                timeout_sec=100,
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
        "\nManually compare the proposals above against the 9 ground-truth items listed at the "
        "top -- this script does not auto-grade that part, real property phrasing has too much "
        "freedom for exact string matching to mean anything."
    )


if __name__ == "__main__":
    main()
