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

Covers the full RV32I base integer set: ALU-reg, ALU-imm, LUI/AUIPC
(rd_wdata correctness + straight-line pc_wdata==pc_rdata+4), the 6
branches (pc_wdata correctness under both taken/not-taken, plus an
explicit "branches never write rd" check), JAL/JALR (rd_wdata==return
address, pc_wdata==target), and loads/stores (mem_addr/mem_rmask/
mem_wmask/mem_rdata/mem_wdata consistency, plus load rd_wdata sign/zero
extension). FENCE and ECALL/EBREAK are deliberately out of scope --
FENCE is a no-op with nothing to check, and ECALL/EBREAK just need
rvfi_trap==1, arguably better covered by a dedicated trap-behavior check
than force-fit into this per-instruction-correctness shape.
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
MEM_ADDR = "rvfi_mem_addr"
MEM_RMASK = "rvfi_mem_rmask"
MEM_WMASK = "rvfi_mem_wmask"
MEM_RDATA = "rvfi_mem_rdata"
MEM_WDATA = "rvfi_mem_wdata"

IMM_I = f"{{{{20{{{INSN}[31]}}}}, {INSN}[31:20]}}"  # sign-extended 12-bit I-type imm
IMM_S = f"{{{{20{{{INSN}[31]}}}}, {INSN}[31:25], {INSN}[11:7]}}"  # S-type (store) imm
IMM_B = f"{{{{19{{{INSN}[31]}}}}, {INSN}[31], {INSN}[7], {INSN}[30:25], {INSN}[11:8], 1'b0}}"  # B-type (branch) imm
IMM_J = f"{{{{11{{{INSN}[31]}}}}, {INSN}[31], {INSN}[19:12], {INSN}[20], {INSN}[30:21], 1'b0}}"  # J-type (JAL) imm


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


def pc_target_check(name, opcode, target_expr, funct3=None, funct7=None):
    guard = match(opcode, funct3, funct7)
    expr = f"!({guard}) || ({PC_WDATA} == ({target_expr}))"
    return (name, expr)


def branch_pc_check(name, funct3, taken_expr):
    guard = match("1100011", funct3)
    expr = (
        f"!({guard}) || ({PC_WDATA} == (({taken_expr}) "
        f"? ({PC_RDATA} + {IMM_B}) : ({PC_RDATA} + 32'd4)))"
    )
    return (name, expr)


def mem_addr_check(name, opcode, imm_expr, funct3=None):
    guard = match(opcode, funct3)
    expr = f"!({guard}) || ({MEM_ADDR} == ({RS1} + ({imm_expr})))"
    return (name, expr)


def mask_expr(funct3_low2, byte_off_expr):
    if funct3_low2 == "00":
        return f"(4'b0001 << ({byte_off_expr}))"
    if funct3_low2 == "01":
        return f"(4'b0011 << ({byte_off_expr}))"
    return "4'b1111"


def load_mask_check(name, funct3):
    guard = match("0000011", funct3)
    byte_off = f"(({RS1} + {IMM_I})[1:0])"
    expr = f"!({guard}) || ({MEM_RMASK} == {mask_expr(funct3[1:3], byte_off)})"
    return (name, expr)


def store_mask_check(name, funct3):
    guard = match("0100011", funct3)
    byte_off = f"(({RS1} + {IMM_S})[1:0])"
    expr = f"!({guard}) || ({MEM_WMASK} == {mask_expr(funct3[1:3], byte_off)})"
    return (name, expr)


def store_wdata_check(name, funct3):
    # dmem_wdata is rs2_rdata shifted into its byte lane -- confirmed from
    # rv32i_core.v's own assign dmem_wdata = rs2_rdata << (byte_off * 8),
    # mirrored into rvfi_mem_wdata unchanged for stores.
    guard = match("0100011", funct3)
    byte_off = f"(({RS1} + {IMM_S})[1:0])"
    expr = f"!({guard}) || ({MEM_WDATA} == ({RS2} << (({byte_off}) * 8)))"
    return (name, expr)


def load_rd_formula(funct3, width, signed):
    byte_off = f"(({RS1} + {IMM_I})[1:0])"
    shifted = f"({MEM_RDATA} >> (({byte_off}) * 8))"
    if width == 8:
        return (f"{{{{24{{{shifted}[7]}}}}, {shifted}[7:0]}}" if signed
                 else f"{{24'd0, {shifted}[7:0]}}")
    if width == 16:
        return (f"{{{{16{{{shifted}[15]}}}}, {shifted}[15:0]}}" if signed
                 else f"{{16'd0, {shifted}[15:0]}}")
    return shifted  # width == 32, LW


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

# ---- B-type branches (opcode 1100011) ----
CHECKS.append(branch_pc_check("insn_beq", "000", f"{RS1} == {RS2}"))
CHECKS.append(branch_pc_check("insn_bne", "001", f"{RS1} != {RS2}"))
CHECKS.append(branch_pc_check("insn_blt", "100", f"$signed({RS1}) < $signed({RS2})"))
CHECKS.append(branch_pc_check("insn_bge", "101", f"$signed({RS1}) >= $signed({RS2})"))
CHECKS.append(branch_pc_check("insn_bltu", "110", f"{RS1} < {RS2}"))
CHECKS.append(branch_pc_check("insn_bgeu", "111", f"{RS1} >= {RS2}"))
CHECKS.append(rd_check("branch_no_rd_write", "1100011", "32'd0"))

# ---- Jumps: JAL (opcode 1101111), JALR (opcode 1100111, funct3 000) ----
CHECKS.append(rd_check("insn_jal_rd", "1101111", f"{PC_RDATA} + 32'd4"))
CHECKS.append(pc_target_check("insn_jal_pc", "1101111", f"{PC_RDATA} + {IMM_J}"))
CHECKS.append(rd_check("insn_jalr_rd", "1100111", f"{PC_RDATA} + 32'd4", "000"))
CHECKS.append(pc_target_check("insn_jalr_pc", "1100111", f"({RS1} + {IMM_I}) & ~32'd1", "000"))

# ---- Loads (opcode 0000011) ----
CHECKS.append(mem_addr_check("load_addr", "0000011", IMM_I))
CHECKS.append(load_mask_check("load_mask_lb", "000"))
CHECKS.append(load_mask_check("load_mask_lh", "001"))
CHECKS.append(load_mask_check("load_mask_lw", "010"))
CHECKS.append(load_mask_check("load_mask_lbu", "100"))
CHECKS.append(load_mask_check("load_mask_lhu", "101"))
CHECKS.append(rd_check("insn_lb", "0000011", load_rd_formula("000", 8, signed=True), "000"))
CHECKS.append(rd_check("insn_lh", "0000011", load_rd_formula("001", 16, signed=True), "001"))
CHECKS.append(rd_check("insn_lw", "0000011", load_rd_formula("010", 32, signed=True), "010"))
CHECKS.append(rd_check("insn_lbu", "0000011", load_rd_formula("100", 8, signed=False), "100"))
CHECKS.append(rd_check("insn_lhu", "0000011", load_rd_formula("101", 16, signed=False), "101"))
CHECKS.append(straight_pc_check("pc_load", "0000011"))

# ---- Stores (opcode 0100011) ----
CHECKS.append(mem_addr_check("store_addr", "0100011", IMM_S))
CHECKS.append(store_mask_check("store_mask_sb", "000"))
CHECKS.append(store_mask_check("store_mask_sh", "001"))
CHECKS.append(store_mask_check("store_mask_sw", "010"))
CHECKS.append(store_wdata_check("store_wdata_sb", "000"))
CHECKS.append(store_wdata_check("store_wdata_sh", "001"))
CHECKS.append(store_wdata_check("store_wdata_sw", "010"))
CHECKS.append(rd_check("store_no_rd_write", "0100011", "32'd0"))
CHECKS.append(straight_pc_check("pc_store", "0100011"))


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
