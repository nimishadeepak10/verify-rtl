# Verification Plan — `rv32i_core.v` (RV32I base integer core)

**Document status:** live — reflects `scripts/test_rv32i_rvfi_checks.py` as of the commit that
adds this file. Regenerate the traceability matrix (§5) whenever a check is added, renamed, or
its verdict changes; don't let this document drift from what the suite actually runs.

**Scope of this plan:** formal verification only, via [SymbiYosys](https://github.com/YosysHQ/sby)
against the [RVFI](https://github.com/YosysHQ/riscv-formal) interface. This project also has a
simulation path (Icarus Verilog) used earlier in `rv32i_core.v`'s development to catch RTL bugs
before any property existed to catch them formally (see §7) — that work is out of scope for this
document, which plans and tracks only what is proven by the solver, not what passed a directed
test vector.

---

## 1. Design under test

| | |
|---|---|
| **DUT** | `examples/rv32i_core.v` |
| **ISA** | RV32I base integer instruction set (unprivileged spec), no compressed instructions |
| **Microarchitecture** | Single-cycle, single-issue (RVFI `NRET=1`), `XLEN=32`, `ILEN=32` |
| **Memory model** | Harvard-style, external combinational-read `imem`/`dmem` (environment returns data same-cycle) |
| **Privileged state** | None — no CSR file, no privilege modes. Any SYSTEM-opcode instruction (ECALL/EBREAK and anything else encoding to `1110011`) is treated as an unconditional trap rather than distinguished or executed |
| **Trap model** | On trap (illegal instruction, misaligned jump/branch target, or any SYSTEM instruction), the core halts in place (`pc_next = pc`) — no trap handler is modeled |
| **Verification interface** | RVFI, wired per the official spec (`docs/source/rvfi.rst` in riscv-formal), driven combinationally from the same signals that drive the state update each cycle |

## 2. Verification methodology

Every property in this plan is a **same-cycle RVFI check**: given the instruction word and its
pre-state operands (exposed by RVFI on the cycle that instruction retires), the property asserts
the post-state result RVFI also exposes on that same cycle. This is deliberate, not incidental —
it is the direct answer to the same-cycle-only ceiling this project's earlier, simpler designs
(a FIFO, a divider, a cache) ran into for anything genuinely multi-cycle: RVFI turns
"is this one instruction correct" into a same-cycle claim by construction, so it sits inside what
this project's formal pipeline can express, not at its edge.

**Tool chain:**
- [Yosys](https://github.com/YosysHQ/yosys) — synthesis front-end, reads the RTL + a generated
  formal wrapper (`generate_formal_wrapper()` in `src/rtl_verify/formal_props.py`) that emits each
  property as an immediate SystemVerilog assertion (grammar confirmed against IEEE 1800-2023 §16.3
  — see `docs/systemverilog_ieee1800_rules.md` §3).
- [SymbiYosys (sby)](https://github.com/YosysHQ/sby) — orchestrates the proof.
- Solvers: `abc pdr` (unbounded property-directed reachability) as the primary engine for this
  sequential design, falling back through `smtbmc` with the `yices` and `z3` SMT backends
  (k-induction) if PDR doesn't produce a definitive PASS/FAIL — see
  `recommended_engine_chain()` in `src/rtl_verify/formal_props.py` for the exact fallback logic
  and rationale (genuine engine/algorithm diversity, not just a config knob).

**Verdict discipline:** a property is only reported PROVEN if the solver itself returns PASS.
FALSIFIED means a real counterexample trace exists (inspected via VCD before being trusted — see
§7 for two cases where a "falsified" property turned out to be a bug in the property, not the
core). No verdict in this plan is inferred or assumed.

**Runner:** `scripts/test_rv32i_rvfi_checks.py` — running it end to end reproduces every verdict
in §5.

## 3. Features requiring verification

Derived directly from the RV32I base ISA this core implements, grouped by instruction format:

| # | Feature group | What must be true, per-instruction |
|---|---|---|
| F1 | R-type ALU ops (ADD/SUB/SLL/SLT/SLTU/XOR/SRL/SRA/OR/AND) | `rd_wdata` matches the ISA-defined result; `rd_wdata==0` when `rd==x0`; `pc_wdata==pc+4` |
| F2 | I-type ALU-imm ops (ADDI/SLTI/SLTIU/XORI/ORI/ANDI/SLLI/SRLI/SRAI) | same as F1, operand 2 is the sign-extended immediate |
| F3 | LUI / AUIPC | `rd_wdata` matches the ISA-defined result (`{imm,12'd0}` / `pc+{imm,12'd0}`); `pc_wdata==pc+4` |
| F4 | Conditional branches (BEQ/BNE/BLT/BGE/BLTU/BGEU) | `pc_wdata` is the branch target when taken and untrapped, else `pc+4`; branches never write `rd` |
| F5 | JAL / JALR | `rd_wdata==pc+4` (return address); `pc_wdata==` the ISA-defined jump target when untrapped |
| F6 | Loads (LB/LH/LW/LBU/LHU) | `mem_addr`, byte `mem_rmask`, and `rd_wdata` (with correct sign/zero extension per width) all match the ISA definition; `pc_wdata==pc+4` |
| F7 | Stores (SB/SH/SW) | `mem_addr`, byte `mem_wmask`, and `mem_wdata` (correctly byte-lane-shifted) all match the ISA definition; stores never write `rd`; `pc_wdata==pc+4` |
| F8 | FENCE | No-op: never traps, never writes `rd`, `pc_wdata==pc+4` |
| F9 | SYSTEM (ECALL/EBREAK and any other `1110011` encoding) | Always traps (`rvfi_trap==1`); never writes `rd`; `pc_wdata==pc_rdata` (halts in place) |
| F10 | Trap-on-misalignment | A taken branch or JAL/JALR whose target is not 4-byte-aligned traps instead of jumping (folded into F4/F5's `!rvfi_trap` guard, not a separate property group — see §5) |

**Deliberately not a feature of this core, and so not in this plan:** compressed instructions,
CSRs/privileged mode, interrupts, and multi-issue/pipeline hazards — none exist in the RTL, so
there is nothing for a property to assert.

## 4. Coverage argument

Every opcode this core decodes (`is_rtype` through `is_system` in `rv32i_core.v`) has at least one
property in §5 that exercises it, and every RVFI output port (`rvfi_trap`, `rvfi_rd_wdata`,
`rvfi_pc_wdata`, `rvfi_mem_addr`, `rvfi_mem_rmask`, `rvfi_mem_wmask`, `rvfi_mem_rdata`,
`rvfi_mem_wdata`) is checked by at least one property. `funct3`/`funct7` sub-decodes are checked
individually (e.g. all 10 R-type ALU ops, all 6 branch conditions, all 5 load widths/signs) rather
than as one guard covering the opcode — a single formula bug in one ALU op would otherwise hide
behind nine correct ones. This is exhaustive over the *encoding space* the core implements (PDR is
an unbounded proof, not a bounded sample of instruction sequences), not exhaustive over every
possible RTL bug class — see §8 for what this plan does not claim.

## 5. Traceability matrix

Status current as of the last full run of `scripts/test_rv32i_rvfi_checks.py` (57 base checks +
6 FENCE/SYSTEM checks = 63 total). ✅ PROVEN means the solver returned PASS; every row below is
currently ✅.

| Feature | Property (check name) | Verifies |
|---|---|---|
| F1 | `insn_add`, `insn_sub`, `insn_sll`, `insn_slt`, `insn_sltu`, `insn_xor`, `insn_srl`, `insn_sra`, `insn_or`, `insn_and`, `pc_add` | rd_wdata per op + pc_wdata==pc+4 |
| F2 | `insn_addi`, `insn_slti`, `insn_sltiu`, `insn_xori`, `insn_ori`, `insn_andi`, `insn_slli`, `insn_srli`, `insn_srai`, `pc_addi` | rd_wdata per op + pc_wdata==pc+4 |
| F3 | `insn_lui`, `insn_auipc`, `pc_lui`, `pc_auipc` | rd_wdata + pc_wdata==pc+4 |
| F4 | `insn_beq`, `insn_bne`, `insn_blt`, `insn_bge`, `insn_bltu`, `insn_bgeu`, `branch_no_rd_write` | pc_wdata under taken/not-taken (untrapped) + no rd write |
| F5 | `insn_jal_rd`, `insn_jal_pc`, `insn_jalr_rd`, `insn_jalr_pc` | rd_wdata==return addr + pc_wdata==target (untrapped) |
| F6 | `load_addr`, `load_mask_lb/lh/lw/lbu/lhu`, `insn_lb/lh/lw/lbu/lhu`, `pc_load` | mem_addr, per-width mem_rmask, rd_wdata sign/zero extension, pc_wdata==pc+4 |
| F7 | `store_addr`, `store_mask_sb/sh/sw`, `store_wdata_sb/sh/sw`, `store_no_rd_write`, `pc_store` | mem_addr, per-width mem_wmask/mem_wdata, no rd write, pc_wdata==pc+4 |
| F8 | `fence_no_trap`, `fence_no_rd_write`, `pc_fence` | never traps, never writes rd, pc_wdata==pc+4 |
| F9 | `system_always_traps`, `system_no_rd_write`, `system_pc_frozen` | always traps, never writes rd, pc_wdata==pc_rdata |
| F10 | Folded into F4/F5 (`!rvfi_trap` guard in `branch_pc_check`/`pc_target_check`) | trap-on-misalignment doesn't falsely falsify the taken-branch/jump-target properties |

Run `py scripts/test_rv32i_rvfi_checks.py` to reproduce; its printed summary line
(`proven=N falsified=0 error=0 inconclusive=0`) is the authoritative current count — update it
here if it ever changes.

## 6. Sign-off criteria

This DUT is considered formally signed off when:
1. Every row in §5 exists and is ✅ PROVEN (not just non-FALSIFIED — `inconclusive`/`ERROR`
   verdicts do not count as sign-off).
2. Every opcode `rv32i_core.v` decodes has coverage per §4's argument.
3. No open item in §7 represents an unresolved *design* bug (property-authoring bugs found and
   fixed along the way are expected and documented, not blocking).

As of this document, all three conditions hold for the RV32I base ISA this core implements.

## 7. Bugs found by this plan (for traceability, not narrative)

- **Ternary-signedness bug (RTL):** SRA/SRAI's arithmetic shift silently degraded to a logical
  shift when written as `cond ? ($signed(x) >>> n) : (x >> n)` — IEEE 1800-2023 §11.8.1. Found in
  simulation first, then hit again in property authoring via the equality operator. Full root
  cause: `docs/systemverilog_ieee1800_rules.md` §1.
- **Missing RVFI write-suppression invariant (property bug):** an early `rd_wdata` property
  compared against the raw ALU formula unconditionally; PDR immediately found the real
  counterexample that `rd_wdata` must be 0 when `rd==x0`. Fixed in `rd_check()`.
- **Missing trap-freeze guard (property bug):** branch/jump `pc_wdata` properties were falsified
  by a real PDR counterexample (a taken branch to a misaligned target) — not a core bug, but the
  property not accounting for `rv32i_core.v`'s own correct trap-on-misalignment behavior (halting
  at `pc_next==pc` instead of jumping). Fixed with an explicit `!rvfi_trap` guard in
  `branch_pc_check()`/`pc_target_check()`.
- **Yosys frontend limitation — bit-select on a non-identifier expression:** `(a + b)[1:0]` is a
  real syntax error in yosys's Verilog frontend (part-selects only apply to identifiers), not a
  semantic issue. Every non-word-sized load/store check hit this identically. Fixed by expressing
  byte offsets and sign/zero extension with `&`/`|` masks instead of part-selects.

## 8. Explicit non-goals

This plan proves per-instruction functional correctness of a single-issue, single-cycle core with
no privileged state, against RVFI's same-cycle contract. It does **not** claim:
- Timing/physical verification (this is RTL-level formal, not gate-level or STA).
- Verification of a multi-issue or pipelined implementation — this core has neither.
- CSR, interrupt, or privileged-mode correctness — none exist in this RTL.
- Compressed-instruction (RVC) decoding — not implemented.
- Liveness/progress properties (e.g. "the core always eventually retires an instruction") —
  same-cycle assertions don't express these; see this project's README `Limitations` section for
  why that's a deliberate pipeline-wide boundary, not specific to this DUT.
