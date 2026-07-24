"""End-to-end check: RTL -> generated SVA checker -> SymbiYosys backend.

Uses the traffic light FSM (examples/traffic_light_fsm.v). `light` is only
ever assigned S_RED (0) or S_GREEN (1), so `light <= 1` is a real safety
property that should PASS, and `light == 0` (always red) should FAIL with
a counterexample.
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


def run_case(label: str, rtl_path: Path, module_name: str, prop_expr: str, expect_pass: bool) -> None:
    rtl_source = rtl_path.read_text(encoding="utf-8")
    module = analyze_rtl(rtl_source, top_module=module_name)

    wrapper_sv = generate_formal_wrapper(module, [("prop0", prop_expr)])

    work = Path(tempfile.mkdtemp(prefix=f"formal_{label}_"))
    wrapper_path = work / "wrapper.sv"
    wrapper_path.write_text(wrapper_sv, encoding="utf-8")

    backend = SymbiYosysBackend()
    result = backend.run(rtl_path, wrapper_path, work, top=f"{module_name}_formal_top", depth=6)

    status = "PASS" if result.success else "FAIL"
    ok = result.success == expect_pass
    print(f"[{label}] expected={'PASS' if expect_pass else 'FAIL'} got={status} "
          f"{'OK' if ok else 'MISMATCH'} (trace={result.vcd_path})")
    if not ok:
        print(result.log[-1500:])
        raise SystemExit(1)


def main() -> None:
    rtl_path = ROOT / "examples" / "traffic_light_fsm.v"
    backend = SymbiYosysBackend()
    print("SymbiYosys available:", backend.is_available(), "version:", backend.version())

    run_case("light_never_invalid", rtl_path, "traffic_light_fsm", "light <= 1", expect_pass=True)
    run_case("light_always_red", rtl_path, "traffic_light_fsm", "light == 0", expect_pass=False)
    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
