"""Assumption consistency ("over-constraint") checking -- researched from
real industry guidance (EDN's FPV over-constraint write-up, Oski
Technology's published minimal-constraint-set debugging technique) before
being implemented; see src/rtl_verify/assumption_check.py's own module
docstring for the full citation trail. Real, solver-based tests (no
synthetic mocking of the solver itself) -- must be run via PowerShell,
not the Bash tool, per this project's own persistent memory note on
yosys subprocess spawning.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.assumption_check import check_assumption_consistency  # noqa: E402
from rtl_verify.backends.symbiyosys import SymbiYosysBackend  # noqa: E402


def _module_and_rtl_path(source: str, top: str, work: Path) -> tuple:
    mod = analyze_rtl(source, top_module=top)
    rtl_path = work / "dut.v"
    rtl_path.write_text(source, encoding="utf-8")
    return mod, rtl_path


def main() -> None:
    source = (ROOT / "examples" / "sync_fifo.v").read_text(encoding="utf-8")
    backend = SymbiYosysBackend()

    print("=== A genuinely consistent, real assumption -> expect CONSISTENT ===")
    work1 = Path(tempfile.mkdtemp(prefix="assume_test1_"))
    mod1, rtl1 = _module_and_rtl_path(source, "sync_fifo", work1)
    result1 = check_assumption_consistency(
        mod1, rtl1, backend,
        assume_props=[("wr_en_sane", "wr_en || !wr_en", "assume")],  # tautology, always satisfiable
        timeout_sec=60, work_root=work1 / "check",
    )
    print(f"  status: {result1.status}")
    print(f"  note: {result1.note[:200]}")
    assert result1.status == "CONSISTENT", result1
    print("OK\n")

    print("=== A single, directly self-contradictory assumption -> expect "
          "OVER_CONSTRAINED, isolated to itself ===")
    work2 = Path(tempfile.mkdtemp(prefix="assume_test2_"))
    mod2, rtl2 = _module_and_rtl_path(source, "sync_fifo", work2)
    result2 = check_assumption_consistency(
        mod2, rtl2, backend,
        assume_props=[("impossible", "wr_en && !wr_en", "assume")],  # contradiction on its own
        timeout_sec=60, work_root=work2 / "check",
    )
    print(f"  status: {result2.status}")
    print(f"  minimal_conflicting_set: {result2.minimal_conflicting_set}")
    assert result2.status == "OVER_CONSTRAINED", result2
    assert result2.minimal_conflicting_set == ["impossible"], result2
    print("OK\n")

    print("=== Two assumptions, individually fine, contradictory ONLY together -> "
          "expect OVER_CONSTRAINED, isolated to BOTH, not either alone ===")
    work3 = Path(tempfile.mkdtemp(prefix="assume_test3_"))
    mod3, rtl3 = _module_and_rtl_path(source, "sync_fifo", work3)
    result3 = check_assumption_consistency(
        mod3, rtl3, backend,
        assume_props=[
            ("wr_en_always", "wr_en", "assume"),
            ("wr_en_never", "!wr_en", "assume"),
            ("unrelated_ok", "rd_en || !rd_en", "assume"),  # a real, harmless third assumption
        ],
        timeout_sec=60, work_root=work3 / "check",
    )
    print(f"  status: {result3.status}")
    print(f"  minimal_conflicting_set: {result3.minimal_conflicting_set}")
    assert result3.status == "OVER_CONSTRAINED", result3
    assert set(result3.minimal_conflicting_set) == {"wr_en_always", "wr_en_never"}, (
        "the harmless third assumption must be excluded from the isolated conflict set", result3
    )
    print("OK\n")

    print("=== No assumptions supplied -> NOT_APPLICABLE, no solver call ===")
    result4 = check_assumption_consistency(mod1, rtl1, backend, assume_props=[])
    assert result4.status == "NOT_APPLICABLE" and result4.checked is False, result4
    print("OK\n")

    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
