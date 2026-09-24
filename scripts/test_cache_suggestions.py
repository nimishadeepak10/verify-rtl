"""Stage 6 of the complexity roadmap: cache.

direct_cache.v is a direct-mapped, write-through, write-no-allocate
cache (4 lines, 1 byte/line) with a multi-cycle miss-fill FSM talking to
an external memory interface -- the next rung after divider8.v/
multiplier32.v (complex math) and the first design with both tag/valid
arrays AND an unconstrained external responder (mem_ready/mem_rdata).

Functionally verified correct first against a real Icarus simulation of
7 cases (miss, hit, write-hit with write-through visibility, tag-mismatch
-on-same-index eviction, and write-no-allocate semantics) -- catching a
real testbench race condition in the process (blocking-assignment
stimulus racing the DUT's own posedge sampling), fixed and re-confirmed
before trusting any formal result against the design.

Originally found only 1 of 11 suggested properties expressible, because
the wrapper only ever wired up the DUT's own ports -- the tag/valid
arrays that make this design interesting are internal registers, with no
port a property could reference. dut_probe.py closes that gap: this
script now instruments the RTL (a generated COPY -- the file on disk is
never touched) to expose every internal register as a real debug port
before suggesting/converting/running, exactly the way api/main.py's
formal endpoints now always do. Compare this run's expressible count
against the "1 of 11" figure in the README to see the gap actually close.

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
from rtl_verify.dut_probe import generate_probed_rtl  # noqa: E402
from rtl_verify.formal_props import generate_formal_wrapper, recommended_engine_chain  # noqa: E402
from rtl_verify.property_suggester import suggest_properties  # noqa: E402
from rtl_verify.property_to_sva import convert_to_sva  # noqa: E402

GROUND_TRUTH = """
Known-correct properties for this direct-mapped write-through cache (for
manual comparison against the suggestions below):

  SAME-CYCLE (should be proposed AND expressible):
    1. hit implies ready in the same cycle (by construction -- hit is
       only ever set alongside ready, on both the fast read-hit path and
       the delayed FILL-completion path)
    2. mem_req and ready are never both high in the same cycle (ready
       only pulses the cycle AFTER mem_req deasserts, since both update
       via nonblocking assignment on the same edge)

  MULTI-CYCLE (should be proposed, but honestly DECLINED at conversion --
  these are exactly the "write followed by a later read" and "history of
  the cache line" claims that need auxiliary/shadow-memory reasoning,
  not just a same-cycle boolean):
    3. a write followed by a later read to the same address returns the
       written value (write-through correctness)
    4. rdata on a hit reflects the value from the most recent write to
       that address (cache coherence with the backing store)
    5. every outstanding request eventually reaches ready (liveness)

  REACHABILITY (cover):
    6. hit is reachable (note: requires a prior miss to populate the line
       first -- a genuinely 2-request sequence, same-cycle-expressible
       target ("hit==1"), multi-cycle to actually reach, same shape as
       the FIFO's "full" cover in Stage 2)
    7. mem_req is reachable (trivial, any miss triggers it)

  INTERNAL-SIGNAL, SAME-CYCLE (newly expressible via dut_probe.py --
  these were previously impossible to even STATE, since tag_arr/
  valid_arr/data_arr/cstate have no port; not a multi-cycle limitation,
  a visibility one):
    8. every tag_arr entry fits its declared 6-bit field regardless of
       valid_arr (structural, holds even on garbage/reset entries)
    9. cstate only ever encodes IDLE (0) or FILL (1) -- never an
       out-of-range value on a 1-bit reg, but worth stating explicitly
       for a wider control register in a bigger design
"""


def main() -> None:
    rtl_path = ROOT / "examples" / "direct_cache.v"
    original_source = rtl_path.read_text(encoding="utf-8")
    base_module = analyze_rtl(original_source, top_module="direct_cache")

    # Instrument: expose every internal register (tag_arr, valid_arr,
    # data_arr, cstate, r_index, r_tag, r_we, r_hit) as a real debug port
    # on a generated copy -- rtl_path itself is never touched.
    probed_source, module, probed = generate_probed_rtl(original_source, base_module)
    work_root = Path(tempfile.mkdtemp(prefix="cache_e2e_probed_src_"))
    rtl_path = work_root / "direct_cache_probed.v"
    rtl_path.write_text(probed_source, encoding="utf-8")
    print(f"Instrumented {len(probed)} internal registers as debug ports: "
          f"{[p.source.name for p in probed]}\n")

    print(GROUND_TRUTH)
    print("=== Suggesting properties (LLM call 1) ===")
    proposals = suggest_properties(module, probed_source, spec_text="")
    print(f"Got {len(proposals)} proposals:\n")
    for p in proposals:
        print(f"[{p['kind']:6s}] ({p['pattern']}) {p['description']}")
        print(f"         signals={p['signals']} paired_cover={p.get('paired_cover')!r}")
    assert len(proposals) >= 1, "expected at least one proposal -- no upper bound, per property_suggester.py's no-cap policy"

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
            work = Path(tempfile.mkdtemp(prefix=f"cache_e2e_{p['name']}_engine{i}_"))
            wrapper_path = work / "wrapper.sv"
            wrapper_path.write_text(wrapper_sv, encoding="utf-8")
            result = backend.run(
                rtl_path, wrapper_path, work,
                top="direct_cache_formal_top",
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
        "\nManually compare against the 7 ground-truth items above. Any FALSIFIED result should "
        "be cross-checked against a counterexample trace before trusting it as a real bug -- "
        "Stage 5 found two FALSIFIED verdicts that turned out to be property-phrasing artifacts, "
        "not real design bugs."
    )


if __name__ == "__main__":
    main()
