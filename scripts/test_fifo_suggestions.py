"""Stage 2 of the complexity roadmap: validate the suggestion engine on a
real FIFO, not another toy FSM/adder — and check its output against a
hand-written, known-correct property set instead of just eyeballing
plausibility.

FIFOs are the standard formal-verification teaching example (riscv-formal,
ZipCPU, and the SymbiYosys docs all use one) precisely because there's an
established ground truth to grade a suggestion engine against:

  same-cycle invariants (this tool's conversion engine should express these):
    - count <= 4                      (occupancy never exceeds capacity)
    - !(full && empty)                (mutually exclusive, this depth > 0)
    - full  == (count == 4)           (full flag matches occupancy exactly)
    - empty == (count == 0)           (empty flag matches occupancy exactly)

  genuinely multi-cycle claims (this tool should DECLINE these, not guess
  a same-cycle approximation — see property_to_sva.py's documented
  same-cycle-only limitation):
    - no data loss / FIFO ordering (data written is eventually read, in order)
    - overflow protection (a write while full does not corrupt/advance state)

  reachability (cover, same-cycle expression + multi-cycle search — the
  search depth is the solver's problem, not the expression's):
    - full is reachable
    - empty is reached again after the FIFO has been filled

Real LLM calls, real SymbiYosys runs — not mocked, same as every other
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
from rtl_verify.formal_props import generate_formal_wrapper, recommended_formal_config  # noqa: E402
from rtl_verify.property_suggester import suggest_properties  # noqa: E402
from rtl_verify.property_to_sva import convert_to_sva  # noqa: E402

GROUND_TRUTH = """
Known-correct properties a competent verification engineer would write for
this FIFO (for manual comparison against what the suggestion engine
actually proposes below — not an automated match, a real design has too
much freedom in phrasing for that to be meaningful):

  SAME-CYCLE (should be proposed AND expressible):
    1. count <= 4                — occupancy never exceeds capacity
    2. !(full && empty)          — mutually exclusive at this depth
    3. full  == (count == 4)     — full flag matches occupancy exactly
    4. empty == (count == 0)     — empty flag matches occupancy exactly

  MULTI-CYCLE (should be proposed, but honestly DECLINED at conversion —
  not approximated):
    5. no data loss / FIFO ordering across writes and reads
    6. a write while full does not corrupt state or silently advance

  REACHABILITY (cover):
    7. full is reachable
    8. empty is reached again after the FIFO has been filled
"""


def main() -> None:
    rtl_path = ROOT / "examples" / "sync_fifo.v"
    rtl_source = rtl_path.read_text(encoding="utf-8")
    module = analyze_rtl(rtl_source, top_module="sync_fifo")

    print(GROUND_TRUTH)
    print("=== Suggesting properties (LLM call 1) ===")
    proposals = suggest_properties(module, rtl_source, spec_text="")
    print(f"Got {len(proposals)} proposals:\n")
    for p in proposals:
        print(f"[{p['kind']:6s}] ({p['pattern']}) {p['description']}")
        print(f"         signals={p['signals']} paired_cover={p.get('paired_cover')!r}")
    assert 1 <= len(proposals) <= 15, f"unexpected proposal count: {len(proposals)}"
    assert all(p["kind"] in ("assert", "assume", "cover") for p in proposals)

    print("\n=== Converting each to SVA (LLM call 2) and running through SymbiYosys ===\n")
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
        config = recommended_formal_config(module, kind=p["kind"])

        work = Path(tempfile.mkdtemp(prefix=f"fifo_e2e_{p['name']}_"))
        wrapper_path = work / "wrapper.sv"
        wrapper_path.write_text(wrapper_sv, encoding="utf-8")

        result = backend.run(
            rtl_path, wrapper_path, work,
            top="sync_fifo_formal_top",
            depth=config["depth"], mode=config["mode"], engine=config["engine"],
        )
        if result.status == "PASS":
            n_proven += 1
            verdict = "REACHED" if p["kind"] == "cover" else "PROVEN"
        elif result.status == "FAIL":
            n_falsified += 1
            verdict = "UNREACHED" if p["kind"] == "cover" else "FALSIFIED"
        else:
            n_inconclusive += 1
            verdict = result.status
        print(f"    -> ran under {config} -> {verdict} (status={result.status}, trace={result.vcd_path})\n")

    print("=== Summary ===")
    print(f"proposed={len(proposals)} expressible={n_expressible} declined={n_declined}")
    print(f"proven/reached={n_proven} falsified/unreached={n_falsified} inconclusive={n_inconclusive}")
    print(
        "\nManually compare the proposals above against the 8 ground-truth items listed at the "
        "top — this script does not auto-grade that part, real property phrasing has too much "
        "freedom for exact string matching to mean anything."
    )


if __name__ == "__main__":
    main()
