"""Cone-of-influence / verified-surface analysis -- Step 3 of the
published "Seven Steps of Formal Signoff" methodology (SemiEngineering,
the OneSpin/Siemens formal verification team). Researched alongside this
project's other formal-methodology features (vacuity, assumption
consistency, mutation adequacy): checking that published 7-step
methodology against what already existed here found three of the seven
steps already independently implemented (spec_traceability.py for step
1, assumption_check.py for step 4, mutation_adequacy.py for step 7) --
this closes step 3, the remaining implementable piece. Structural-only,
no solver involved.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.signal_coverage import analyze_signal_coverage  # noqa: E402


def main() -> None:
    source = (ROOT / "examples" / "sync_fifo.v").read_text(encoding="utf-8")
    mod = analyze_rtl(source, top_module="sync_fifo")

    print("=== A single narrow property -> most ports correctly flagged uncovered ===")
    report = analyze_signal_coverage(mod, [("count_bound", "count <= 4", "assert")])
    print(f"  covered={report.covered_ports} coverage={report.coverage_percent:.0f}%")
    assert report.covered_ports == ["count"], report.covered_ports
    assert "wr_en" in report.uncovered_ports and "rd_en" in report.uncovered_ports, report
    # Clock is excluded from the scored universe entirely -- referencing
    # the clock signal in a property expression isn't what "verified"
    # means here, and it should never appear in either list.
    assert "clk" not in report.covered_ports and "clk" not in report.uncovered_ports, report
    print("OK\n")

    print("=== A property set that references every real port -> 100% coverage ===")
    full_props = [
        ("wr_ok", "wr_en || !wr_en", "assert"),
        ("rd_ok", "rd_en || !rd_en", "assert"),
        ("data_ok", "wr_data == wr_data", "assert"),
        ("rdata_ok", "rd_data == rd_data", "assert"),
        ("full_ok", "full || !full", "assert"),
        ("empty_ok", "empty || !empty", "assert"),
        ("count_bound", "count <= 4", "assert"),
        ("reset_seen", "rst_n || !rst_n", "assume"),
    ]
    report2 = analyze_signal_coverage(mod, full_props)
    print(f"  coverage={report2.coverage_percent:.0f}% uncovered={report2.uncovered_ports}")
    assert report2.uncovered_ports == [], report2.uncovered_ports
    assert report2.coverage_percent == 100.0, report2
    print("OK\n")

    print("=== No properties at all -> 0% coverage, no crash ===")
    report3 = analyze_signal_coverage(mod, [])
    assert report3.coverage_percent == 0.0
    assert report3.covered_ports == []
    print("OK\n")

    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
