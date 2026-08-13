"""Stage 7 of the complexity roadmap: a real RV32I core, verified via a
real RVFI (RISC-V Formal Interface) per riscv-formal's own spec.

Unlike every prior stage, these properties are hand-written, not LLM-
suggested: CPU-level per-instruction correctness is squarely outside what
a black-box RTL suggester can reasonably infer, and that was never the
point of this stage -- the point is RVFI itself. RVFI exists precisely to
turn "is this ONE instruction correct" into a SAME-CYCLE check: every
signal needed (the instruction word, its pre-state operands, and its
post-state results) is exposed together on the cycle that instruction
retires. That is the direct answer to the same-cycle-only ceiling every
earlier stage in this project ran into on multi-cycle designs (the FIFO's
ordering claims, the divider's live-port-vs-captured-value mismatch, the
cache's 1/11 expressibility) -- not a workaround, the industry's actual
answer to it.

Each check has the shape:

    !(rvfi_valid && <rvfi_insn encoding matches this instruction>)
    || (rvfi_rd_wdata == <formula over rvfi_rs1_rdata/rvfi_rs2_rdata/insn>)

run as an ordinary same-cycle `assert` through this project's existing
generate_formal_wrapper() + recommended_engine_chain() infrastructure --
no new checking framework, just RVFI's real signal names and semantics
feeding the same pipeline every other stage used.

This first pass covers the ALU-reg, ALU-imm, and LUI/AUIPC groups (21
instructions) plus a straight-line pc_wdata==pc_rdata+4 check for each,
since none of them branch. Loads/stores/branches/jumps are a deliberately
separate follow-up group, not attempted here.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.backends.symbiyosys import SymbiYosysBackend  # noqa: E402
from rtl_verify.formal_props import generate_formal_wrapper, recommended_engine_chain  # noqa: E402

RS1 = "rvfi_rs1_rdata"
RS2 = "rvfi_rs2_rdata"
INSN = "rvfi_insn"
RD_WDATA = "rvfi_rd_wdata"
RD_ADDR = "rvfi_rd_addr"
PC_RDATA = "rvfi_pc_rdata"
PC_WDATA = "rvfi_pc_wdata"

IMM_I = f"{{{{20{{{INSN}[31]}}}}, {INSN}[31:20]}}"  # sign-extended 12-bit I-type imm


def arith_shift_expr(val, shamt):
    """32-bit arithmetic right shift without $signed() -- see rd_check's
    comment for why: yosys's formal frontend doesn't correctly translate
    $signed(...) >>> ... inside a property expression, confirmed against
    an isolated trivially-true self-check. Standard bit-trick instead:
    logical-shift, then OR in a mask of 1s in exactly the vacated upper
    bits when the sign bit was set (mask is 0 when the value was
    non-negative, since ~(0xFFFFFFFF >> n) is 0 there beyond that)."""
    return f"(({val} >> ({shamt})) | ({val}[31] ? ~(32'hFFFFFFFF >> ({shamt})) : 32'd0))"


def match(opcode, funct3=None, funct7=None):
    parts = [f"rvfi_valid", f"{INSN}[6:0] == 7'b{opcode}"]
    if funct3 is not None:
        parts.append(f"{INSN}[14:12] == 3'b{funct3}")
    if funct7 is not None:
        parts.append(f"{INSN}[31:25] == 7'b{funct7}")
    return " && ".join(parts)


def rd_check(name, opcode, formula, funct3=None, funct7=None):
    guard = match(opcode, funct3, funct7)
    # RVFI's own invariant (confirmed against the real spec, and empirically
    # against a real PDR counterexample): rd_wdata must be zero whenever
    # rd_addr is x0 -- writes to x0 are always suppressed. A first version
    # of this check compared rd_wdata against the raw formula
    # unconditionally and PDR immediately falsified all 21 rd_wdata checks
    # with a real counterexample (rd_addr=0, rs1/rs2 huge and nonzero,
    # rd_wdata correctly 0) -- a bug in the property, not the core.
    #
    # First fix attempt was a ternary, `(rd_addr==0) ? 32'd0 : (formula)` --
    # still failed for insn_sra/insn_srai specifically. Restructuring into
    # two separate implications (below) also didn't help, which disproved
    # the initial hypothesis (a ternary-signedness gotcha like the one
    # found earlier in the core itself). The real cause, confirmed by an
    # isolated minimal probe (a module where `r` is LITERALLY defined as
    # `$signed(a) >>> n`, asserting `r == ($signed(a) >>> n)` -- a
    # trivially-true self-check): yosys's formal/SMT frontend does not
    # correctly translate `$signed(...) >>> ...` inside a property
    # expression at all, even when textually identical to the DUT's own
    # definition. This is narrow, not a broad $signed() problem --
    # `$signed(x) < $signed(y)` (used by insn_slt/insn_slti below) works
    # correctly. Fixed by expressing the arithmetic shift without a signed
    # cast at all (see arith_shift_expr).
    expr = (
        f"(!({guard} && {RD_ADDR} == 5'd0) || ({RD_WDATA} == 32'd0)) && "
        f"(!({guard} && {RD_ADDR} != 5'd0) || ({RD_WDATA} == ({formula})))"
    )
    return (name, expr)


def straight_pc_check(name, opcode, funct3=None, funct7=None):
    guard = match(opcode, funct3, funct7)
    expr = f"!({guard}) || ({PC_WDATA} == ({PC_RDATA} + 32'd4))"
    return (name, expr)


CHECKS = []

# ---- R-type ALU-reg (opcode 0110011) ----
CHECKS.append(rd_check("insn_add", "0110011", f"{RS1} + {RS2}", "000", "0000000"))
CHECKS.append(rd_check("insn_sub", "0110011", f"{RS1} - {RS2}", "000", "0100000"))
CHECKS.append(rd_check("insn_sll", "0110011", f"{RS1} << {RS2}[4:0]", "001", "0000000"))
CHECKS.append(rd_check("insn_slt", "0110011", f"($signed({RS1}) < $signed({RS2})) ? 32'd1 : 32'd0", "010", "0000000"))
CHECKS.append(rd_check("insn_sltu", "0110011", f"({RS1} < {RS2}) ? 32'd1 : 32'd0", "011", "0000000"))
CHECKS.append(rd_check("insn_xor", "0110011", f"{RS1} ^ {RS2}", "100", "0000000"))
CHECKS.append(rd_check("insn_srl", "0110011", f"{RS1} >> {RS2}[4:0]", "101", "0000000"))
CHECKS.append(rd_check("insn_sra", "0110011", arith_shift_expr(RS1, f"{RS2}[4:0]"), "101", "0100000"))
CHECKS.append(rd_check("insn_or", "0110011", f"{RS1} | {RS2}", "110", "0000000"))
CHECKS.append(rd_check("insn_and", "0110011", f"{RS1} & {RS2}", "111", "0000000"))
CHECKS.append(straight_pc_check("pc_add", "0110011", "000", "0000000"))

# ---- I-type ALU-imm (opcode 0010011) ----
CHECKS.append(rd_check("insn_addi", "0010011", f"{RS1} + {IMM_I}", "000"))
CHECKS.append(rd_check("insn_slti", "0010011", f"($signed({RS1}) < $signed({IMM_I})) ? 32'd1 : 32'd0", "010"))
CHECKS.append(rd_check("insn_sltiu", "0010011", f"({RS1} < {IMM_I}) ? 32'd1 : 32'd0", "011"))
CHECKS.append(rd_check("insn_xori", "0010011", f"{RS1} ^ {IMM_I}", "100"))
CHECKS.append(rd_check("insn_ori", "0010011", f"{RS1} | {IMM_I}", "110"))
CHECKS.append(rd_check("insn_andi", "0010011", f"{RS1} & {IMM_I}", "111"))
CHECKS.append(rd_check("insn_slli", "0010011", f"{RS1} << {INSN}[24:20]", "001", "0000000"))
CHECKS.append(rd_check("insn_srli", "0010011", f"{RS1} >> {INSN}[24:20]", "101", "0000000"))
CHECKS.append(rd_check("insn_srai", "0010011", arith_shift_expr(RS1, f"{INSN}[24:20]"), "101", "0100000"))
CHECKS.append(straight_pc_check("pc_addi", "0010011", "000"))

# ---- U-type (opcode 0110111 = LUI, 0010111 = AUIPC) ----
CHECKS.append(rd_check("insn_lui", "0110111", f"{{{INSN}[31:12], 12'd0}}"))
CHECKS.append(rd_check("insn_auipc", "0010111", f"{PC_RDATA} + {{{INSN}[31:12], 12'd0}}"))
CHECKS.append(straight_pc_check("pc_lui", "0110111"))
CHECKS.append(straight_pc_check("pc_auipc", "0010111"))


def main() -> None:
    rtl_path = ROOT / "examples" / "rv32i_core.v"
    rtl_source = rtl_path.read_text(encoding="utf-8")
    module = analyze_rtl(rtl_source, top_module="rv32i_core")
    print(f"Loaded rv32i_core.v: is_sequential={module.is_sequential}, {len(module.ports)} ports")
    print(f"Running {len(CHECKS)} hand-written RVFI checks through the real formal backend\n")

    backend = SymbiYosysBackend()
    n_proven = 0
    n_falsified = 0
    n_inconclusive = 0
    n_error = 0

    for name, expr in CHECKS:
        wrapper_sv = generate_formal_wrapper(module, [(name, expr, "assert")])
        chain = recommended_engine_chain(module, kind="assert")

        result = None
        attempts = []
        for i, config in enumerate(chain):
            work = Path(tempfile.mkdtemp(prefix=f"rv32i_rvfi_{name}_engine{i}_"))
            wrapper_path = work / "wrapper.sv"
            wrapper_path.write_text(wrapper_sv, encoding="utf-8")
            result = backend.run(
                rtl_path, wrapper_path, work,
                top="rv32i_core_formal_top",
                depth=config["depth"], mode=config["mode"], engine=config["engine"],
                timeout_sec=120,
            )
            attempts.append(f"{config['label']}={result.status}")
            if result.status in ("PASS", "FAIL"):
                break

        if result.status == "PASS":
            n_proven += 1
            verdict = "PROVEN"
        elif result.status == "FAIL":
            n_falsified += 1
            verdict = "FALSIFIED"
        elif result.status == "ERROR":
            n_error += 1
            verdict = "ERROR"
        else:
            n_inconclusive += 1
            verdict = result.status
        print(f"{name:14s} [{' -> '.join(attempts)}] -> {verdict}")
        if verdict in ("FALSIFIED", "ERROR"):
            print("    " + "\n    ".join(result.log.splitlines()[-12:]))

    print(f"\n=== Summary: {len(CHECKS)} checks -- proven={n_proven} falsified={n_falsified} "
          f"error={n_error} inconclusive={n_inconclusive} ===")


if __name__ == "__main__":
    main()
