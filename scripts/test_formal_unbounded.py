"""Demonstrates why sequential designs use PDR (unbounded), not shallow BMC.

examples/free_running_counter.v counts up forever with no saturation.
"count < 100" is TRUE for cycles 0-99 and FALSE from cycle 100 onward.

- A shallow BMC check (small depth) never reaches cycle 100, so it reports
  PASS — a real false-positive risk if depth is chosen too small.
- PDR (mode=prove, engine=abc pdr) is not depth-limited: it searches for a
  proof or a genuine counterexample regardless of how far away it is, and
  correctly reports FAIL with a counterexample trace.

Same property, same RTL, two different (correct, for what each engine
actually checks) answers — the point isn't that BMC is "wrong", it's that
"PASS" from a shallow bounded check is a much weaker claim than "PASS"
from an unbounded one, which is exactly why recommended_formal_config()
defaults sequential designs to PDR rather than BMC.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.backends.symbiyosys import SymbiYosysBackend  # noqa: E402
from rtl_verify.formal_props import generate_formal_wrapper  # noqa: E402


def main() -> None:
    rtl_path = ROOT / "examples" / "free_running_counter.v"
    rtl_source = rtl_path.read_text(encoding="utf-8")
    module = analyze_rtl(rtl_source, top_module="free_running_counter")

    wrapper_sv = generate_formal_wrapper(module, [("prop0", "count < 100", "assert")])
    backend = SymbiYosysBackend()

    # 1. Shallow BMC: depth well short of cycle 100 — should (wrongly) PASS.
    work_bmc = Path(tempfile.mkdtemp(prefix="unbounded_bmc_"))
    wrapper_path = work_bmc / "wrapper.sv"
    wrapper_path.write_text(wrapper_sv, encoding="utf-8")
    bmc_result = backend.run(
        rtl_path, wrapper_path, work_bmc,
        top="free_running_counter_formal_top",
        depth=20, mode="bmc", engine="smtbmc",
    )
    print(f"[shallow BMC, depth=20] success={bmc_result.success} "
          f"(expected True — the bug is past this depth, not a false report)")
    assert bmc_result.success is True, "shallow BMC should not have reached the violation"

    # 2. PDR: unbounded — should correctly FAIL with a counterexample.
    work_pdr = Path(tempfile.mkdtemp(prefix="unbounded_pdr_"))
    wrapper_path2 = work_pdr / "wrapper.sv"
    wrapper_path2.write_text(wrapper_sv, encoding="utf-8")
    pdr_result = backend.run(
        rtl_path, wrapper_path2, work_pdr,
        top="free_running_counter_formal_top",
        depth=0, mode="prove", engine="abc pdr",
    )
    print(f"[PDR, unbounded] success={pdr_result.success} trace={pdr_result.vcd_path} "
          f"(expected False — PDR is not limited by depth)")
    assert pdr_result.success is False, "PDR should have found the eventual violation"
    assert pdr_result.vcd_path is not None, "a falsified proof should produce a counterexample trace"

    print("=== CONFIRMED: shallow BMC missed it, PDR caught it — same property, same RTL ===")


if __name__ == "__main__":
    main()
