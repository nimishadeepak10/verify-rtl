"""End-to-end check: RTL -> generated SVA properties -> SymbiYosys backend.

Covers all three example shapes named in the roadmap (Phase 1, task 9):
adder_2bit.v (simple combinational), mux_4to1.v (wider combinational,
if/else chain), traffic_light_fsm.v (sequential/FSM). Also exercises
recommended_formal_config() (Phase 1, task 4) instead of a hardcoded
depth, so combinational designs run as single-step checks and the FSM
runs with a depth derived from its known state count.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.backends.symbiyosys import SymbiYosysBackend  # noqa: E402
from rtl_verify.formal_props import (  # noqa: E402
    generate_formal_wrapper,
    recommended_formal_config,
)


def run_case(label: str, rtl_path: Path, module_name: str, prop_expr: str, expect_pass: bool) -> None:
    rtl_source = rtl_path.read_text(encoding="utf-8")
    module = analyze_rtl(rtl_source, top_module=module_name)

    wrapper_sv = generate_formal_wrapper(module, [("prop0", prop_expr, "assert")])
    config = recommended_formal_config(module)

    work = Path(tempfile.mkdtemp(prefix=f"formal_{label}_"))
    wrapper_path = work / "wrapper.sv"
    wrapper_path.write_text(wrapper_sv, encoding="utf-8")

    backend = SymbiYosysBackend()
    result = backend.run(
        rtl_path,
        wrapper_path,
        work,
        top=f"{module_name}_formal_top",
        depth=config["depth"],
        mode=config["mode"],
        engine=config["engine"],
    )

    status = "PASS" if result.success else "FAIL"
    ok = result.success == expect_pass
    print(
        f"[{label}] expected={'PASS' if expect_pass else 'FAIL'} got={status} "
        f"{'OK' if ok else 'MISMATCH'} (config={config}, trace={result.vcd_path})"
    )
    if not ok:
        print(result.log[-1500:])
        raise SystemExit(1)


def main() -> None:
    backend = SymbiYosysBackend()
    print("SymbiYosys available:", backend.is_available(), "version:", backend.version())

    # Sequential / FSM
    fsm_rtl = ROOT / "examples" / "traffic_light_fsm.v"
    run_case("light_never_invalid", fsm_rtl, "traffic_light_fsm", "light <= 1", expect_pass=True)
    run_case("light_always_red", fsm_rtl, "traffic_light_fsm", "light == 0", expect_pass=False)

    # Simple combinational
    adder_rtl = ROOT / "examples" / "adder_2bit.v"
    run_case("adder_correct", adder_rtl, "adder_2bit", "sum == (a + b)", expect_pass=True)
    run_case("adder_wrong", adder_rtl, "adder_2bit", "sum == (a + b + 1)", expect_pass=False)

    # Wider combinational (if/else chain)
    mux_rtl = ROOT / "examples" / "mux_4to1.v"
    mux_expr = (
        "(sel != 2'b00 || y == d0) && (sel != 2'b01 || y == d1) && "
        "(sel != 2'b10 || y == d2) && (sel != 2'b11 || y == d3)"
    )
    run_case("mux_selects_correct_input", mux_rtl, "mux_4to1", mux_expr, expect_pass=True)
    run_case("mux_always_d0", mux_rtl, "mux_4to1", "y == d0", expect_pass=False)

    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
