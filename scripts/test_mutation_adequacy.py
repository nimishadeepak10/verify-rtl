"""Property-set adequacy via RTL mutation testing -- researched from
YosysHQ's own published MCY (Mutation Cover with Yosys) methodology
before being implemented; see src/rtl_verify/mutate.py and
mutation_adequacy.py's own module docstrings for the full citation
trail and the deliberate simplifications versus the real MCY tool.

Real, solver-based test (no synthetic mocking) -- must be run via
PowerShell, not the Bash tool, per this project's own persistent memory
note on yosys subprocess spawning.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.backends.symbiyosys import SymbiYosysBackend  # noqa: E402
from rtl_verify.mutate import generate_mutants  # noqa: E402
from rtl_verify.mutation_adequacy import run_mutation_adequacy  # noqa: E402

# A tiny 4-op ALU: real, meaningful operator sites for each of the four
# opcodes, deliberately small so a real, solver-based mutation-testing
# run stays fast enough to serve as a committed regression test.
TINY_ALU = """
module tiny_alu (
    input  wire       clk,
    input  wire [1:0] op,
    input  wire [3:0] a,
    input  wire [3:0] b,
    output reg  [3:0] result
);
    always @(posedge clk) begin
        case (op)
            2'b00: result <= a + b;
            2'b01: result <= a - b;
            2'b10: result <= a & b;
            default: result <= a | b;
        endcase
    end
endmodule
"""


def main() -> None:
    mod = analyze_rtl(TINY_ALU, top_module="tiny_alu")
    backend = SymbiYosysBackend()

    print("=== Mutant generation: only real, unambiguous operator sites, "
          "never the nonblocking `<=` itself ===")
    mutants = generate_mutants(mod, TINY_ALU, max_mutants=20)
    print(f"  {len(mutants)} mutants: {[m.operator for m in mutants]}")
    assert len(mutants) == 4, mutants
    assert all("<=" not in m.operator for m in mutants), (
        "the nonblocking assignment operator must never itself be a mutation source", mutants
    )
    print("OK\n")

    print("=== A property that ONLY checks the op=00 (addition) case -> "
          "the other three opcodes' mutations must show up as a REAL, honest gap ===")
    # $past() throughout: `result` is a REGISTERED output, one cycle
    # behind `op`/`a`/`b` -- comparing it combinationally against the
    # CURRENT inputs (a real mistake caught by testing this exact
    # property first) makes it FALSIFIED even on the real, unmutated
    # design, which would make every mutant trivially "CAUGHT" for the
    # wrong reason (the property was already broken, not sensitive to
    # the mutation). generate_formal_wrapper() auto-guards $past()
    # against the first cycle, so no explicit !$initstate() is needed.
    work = Path(tempfile.mkdtemp(prefix="mutation_adequacy_test_"))
    weak_property = [(
        "add_case_correct",
        "($past(op) != 2'b00) || (result == $past(a) + $past(b))",
        "assert",
    )]
    report = run_mutation_adequacy(
        mod, TINY_ALU, proven_properties=weak_property, assume_props=[],
        backend=backend, max_mutants=20, per_attempt_timeout_sec=30,
        work_root=work,
    )
    print(f"  {report.caught} caught, {report.not_caught} not caught, "
          f"{report.inconclusive} inconclusive (of {report.total_mutants})")
    for mr in report.mutants:
        print(f"    {mr.mutant_id} [{mr.operator}] -> {mr.verdict}")
    assert report.checked
    # The op=00 mutation (a + b -> a - b) is the ONLY one this weak
    # property could possibly notice -- everything else is a genuine,
    # honest gap this property set doesn't cover at all.
    op00_mutant = next(mr for mr in report.mutants if "2'b00" in mr.location or mr.operator.startswith("arithmetic: + "))
    assert op00_mutant.verdict == "CAUGHT", op00_mutant
    other_mutants = [mr for mr in report.mutants if mr is not op00_mutant]
    assert all(mr.verdict == "NOT_CAUGHT" for mr in other_mutants), (
        "every mutation outside the op=00 branch must be a real, uncaught gap "
        "for this deliberately narrow property", other_mutants
    )
    assert report.caught == 1 and report.not_caught == 3, report
    print("OK\n")

    print("=== A property set covering ALL FOUR opcodes -> every real "
          "mutation must now be caught ===")
    work2 = Path(tempfile.mkdtemp(prefix="mutation_adequacy_test2_"))
    full_properties = [
        ("add_case_correct", "($past(op) != 2'b00) || (result == $past(a) + $past(b))", "assert"),
        ("sub_case_correct", "($past(op) != 2'b01) || (result == $past(a) - $past(b))", "assert"),
        ("and_case_correct", "($past(op) != 2'b10) || (result == ($past(a) & $past(b)))", "assert"),
        ("or_case_correct", "($past(op) != 2'b11) || (result == ($past(a) | $past(b)))", "assert"),
    ]
    report2 = run_mutation_adequacy(
        mod, TINY_ALU, proven_properties=full_properties, assume_props=[],
        backend=backend, max_mutants=20, per_attempt_timeout_sec=30,
        work_root=work2,
    )
    print(f"  {report2.caught} caught, {report2.not_caught} not caught, "
          f"{report2.inconclusive} inconclusive (of {report2.total_mutants})")
    print(f"  note: {report2.note}")
    assert report2.caught == report2.total_mutants, (
        "a complete, per-opcode property set must catch every one of these real mutations", report2
    )
    assert report2.kill_rate == 1.0, report2
    print("OK\n")

    print("=== No proven properties supplied -> NOT_APPLICABLE, no solver call ===")
    report3 = run_mutation_adequacy(
        mod, TINY_ALU, proven_properties=[], assume_props=[], backend=backend,
    )
    assert report3.checked is False, report3
    print("OK\n")

    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
