"""Cover-statement support: reachable vs unreachable, sequential vs combinational.

Confirmed by actually running it (not assumed): mode="prove" (PDR) silently
folds a `cover` into the proof as a constraint instead of checking
reachability — only mode="cover" makes a cover statement mean anything.
recommended_formal_config(module, kind="cover") picks that mode; this
script locks the behavior in with real runs in both directions.
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


def run_case(label: str, rtl_path: Path, module_name: str, cover_expr: str, expect_reached: bool) -> None:
    rtl_source = rtl_path.read_text(encoding="utf-8")
    module = analyze_rtl(rtl_source, top_module=module_name)

    wrapper_sv = generate_formal_wrapper(module, [("cov0", cover_expr, "cover")])
    config = recommended_formal_config(module, kind="cover")

    work = Path(tempfile.mkdtemp(prefix=f"formal_cover_{label}_"))
    wrapper_path = work / "wrapper.sv"
    wrapper_path.write_text(wrapper_sv, encoding="utf-8")

    backend = SymbiYosysBackend()
    result = backend.run(
        rtl_path, wrapper_path, work,
        top=f"{module_name}_formal_top",
        depth=config["depth"], mode=config["mode"], engine=config["engine"],
    )

    ok = result.success == expect_reached
    print(
        f"[{label}] expected={'REACHED' if expect_reached else 'UNREACHED'} "
        f"got={'REACHED' if result.success else 'UNREACHED'} "
        f"{'OK' if ok else 'MISMATCH'} (config={config}, trace={result.vcd_path})"
    )
    if not ok:
        print(result.log[-1500:])
        raise SystemExit(1)
    if expect_reached:
        assert result.vcd_path is not None, "a reached cover should produce a trace showing how"
    else:
        assert result.vcd_path is None, "an unreached cover has nothing to trace"


def main() -> None:
    fsm_rtl = ROOT / "examples" / "traffic_light_fsm.v"
    run_case("fsm_green_reachable", fsm_rtl, "traffic_light_fsm", "light == 1", expect_reached=True)
    run_case("fsm_invalid_unreachable", fsm_rtl, "traffic_light_fsm", "light == 2'd2", expect_reached=False)

    adder_rtl = ROOT / "examples" / "adder_2bit.v"
    run_case("adder_sum6_reachable", adder_rtl, "adder_2bit", "sum == 3'd6", expect_reached=True)
    run_case("adder_sum7_unreachable", adder_rtl, "adder_2bit", "sum == 3'd7", expect_reached=False)

    print("=== ALL COVER CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
