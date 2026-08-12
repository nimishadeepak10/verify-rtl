"""Stage 5b: direct, hand-written stress test of solver difficulty.

multiplier32.v is purely combinational, so a single BMC step is already
an exhaustive proof regardless of width (see recommended_formal_config's
own documented rationale) -- completeness isn't in question here, only
wall-clock cost. "product == a * b" forces the SAT solver to bit-blast a
32x32 multiplication equivalence check, which can be genuinely slow.

This does NOT go through the LLM suggestion/conversion pipeline -- the
property is hand-written on purpose, since the question here is "does
the solver choke on real arithmetic difficulty," not "does the
suggestion engine propose good properties" (already covered in Stages
2 and 4). Run under a short timeout to see what actually happens,
rather than assuming a TIMEOUT (or a fast PASS) in advance.

Real SymbiYosys run -- not mocked.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.backends.symbiyosys import SymbiYosysBackend  # noqa: E402
from rtl_verify.formal_props import generate_formal_wrapper, recommended_formal_config  # noqa: E402

SHORT_TIMEOUT_SEC = 15


def main() -> None:
    rtl_path = ROOT / "examples" / "multiplier32.v"
    rtl_source = rtl_path.read_text(encoding="utf-8")
    module = analyze_rtl(rtl_source, top_module="multiplier32")
    print(f"is_sequential={module.is_sequential} (expect False -- purely combinational)")

    wrapper_sv = generate_formal_wrapper(module, [("mul_correct", "product == (a * b)", "assert")])
    config = recommended_formal_config(module, kind="assert")
    print(f"recommended config: {config}")

    work = Path(tempfile.mkdtemp(prefix="multiplier_stress_"))
    wrapper_path = work / "wrapper.sv"
    wrapper_path.write_text(wrapper_sv, encoding="utf-8")

    backend = SymbiYosysBackend()
    t0 = time.perf_counter()
    result = backend.run(
        rtl_path, wrapper_path, work,
        top="multiplier32_formal_top",
        depth=config["depth"], mode=config["mode"], engine=config["engine"],
        timeout_sec=SHORT_TIMEOUT_SEC,
    )
    elapsed = time.perf_counter() - t0

    print(f"\nstatus={result.status} success={result.success} elapsed={elapsed:.1f}s (budget={SHORT_TIMEOUT_SEC}s)")
    if result.status == "PASS":
        print("Solved within budget: the SAT solver bit-blasted a 32x32 multiplier equivalence "
              "check and decided it -- genuinely exhaustive (single BMC step, combinational), "
              "not just fast because the property was easy to state.")
    elif result.status == "TIMEOUT":
        print("Genuinely hit the wall-clock budget -- exactly the honest 'we don't know, not a "
              "proof either way' outcome Stage 1 built the TIMEOUT/UNKNOWN machinery for. This "
              "is NOT a bug in this pipeline; it's real solver difficulty on a 32x32 multiply.")
    else:
        print(f"Unexpected status ({result.status}) -- see log:")
        print(result.log[-3000:])


if __name__ == "__main__":
    main()
