# VerifyRTL

**LLM-assisted RTL verification: directed simulation and formal verification in one pipeline, with an emphasis on never reporting a stronger verdict than the tool actually earned.**

VerifyRTL takes a Verilog/SystemVerilog design and automates the parts of verification that usually eat the most engineering time: writing a testbench, picking a formal proof strategy, drafting properties, and triaging failures. It runs on real tools — [Icarus Verilog](https://bleyer.org/icarus/) for simulation, [SymbiYosys](https://github.com/YosysHQ/sby)/[Yosys](https://github.com/YosysHQ/yosys) for formal — and uses an LLM only where it adds real leverage: proposing properties from RTL structure, converting natural-language claims to SVA, and explaining a failing trace. Every verdict is grounded in an actual solver or simulator run; nothing is inferred or guessed.

## The gap this closes: verification plan → properties → verified

A verification plan states *what* a design must guarantee. Turning that into something a solver can actually check — and then running the check — is normally manual, disconnected work spanning specs, waveforms, and tool-specific syntax. That gap, not any single simulation or proof, is what VerifyRTL automates:

1. **What needs verification.** `vplan_builder.py` analyzes the RTL and produces a structured `VerificationPlan` (`vplan.py`): test categories (directed, corner, negative, random, exhaustive) and coverage goals (statement / branch / toggle / FSM / functional), each with an explicit rationale or N/A reason — not a blank template. For formal targets, `property_suggester.py` asks the same "what must this design guarantee" question directly against RTL structure and an optional spec, phrased as candidate assert/assume/cover claims instead of test categories.
2. **Convert to properties.** `property_to_sva.py` turns each candidate into a real SystemVerilog Assertion — and, critically, **declines** to convert anything genuinely multi-cycle rather than silently approximating it as same-cycle (see [Limitations](#limitations)). Only what's honestly checkable becomes a property.
3. **Verify with connected tools.** Every property runs through a real backend — [SymbiYosys](https://github.com/YosysHQ/sby) (BMC / unbounded PDR / k-induction, with the [engine/solver fallback chain](#formal-verification-engines-fallback-and-honesty) below) for formal, [Icarus Verilog](https://bleyer.org/icarus/) for simulation. The verdict is always the tool's own answer — `PROVEN`, `FALSIFIED`, `REACHED`, `UNREACHED`, or an honest `TIMEOUT`/`UNKNOWN` — never inferred.

**Worked example — `sync_fifo.v`** (a 4-entry, 8-bit synchronous FIFO). What a verification plan needs to check, and what actually happened when each claim was pushed through to a real solver:

| What needs verification | Property | Verified by | Result |
|---|---|---|---|
| Occupancy never exceeds capacity | `count <= 4` | SymbiYosys, PDR | **PROVEN** |
| `full` and `empty` are mutually exclusive | `!(full && empty)` | SymbiYosys, PDR | **PROVEN** |
| `full` flag matches occupancy exactly | `full == (count == 4)` | SymbiYosys, PDR | **PROVEN** |
| `empty` flag matches occupancy exactly | `empty == (count == 0)` | SymbiYosys, PDR | **PROVEN** |
| `full` is actually reachable, not just assumed | `cover(full)` | SymbiYosys, BMC | **REACHED** |
| No data loss: writes come back out in order | *(declined — see below)* | — | not approximated |
| A write while full doesn't silently corrupt state | *(declined — see below)* | — | not approximated |

The last two rows are as important as the first five: both are genuinely multi-cycle claims (they require relating a value written on one cycle to a value read several cycles later), and `property_to_sva.py` correctly refuses to convert them into a same-cycle approximation that would look verified but wouldn't actually mean what it claims. Every design in the [stress-testing section](#stress-testing-against-increasing-complexity) below has its own version of this table — see `scripts/test_*_suggestions.py` for the full proposed/expressible/proven breakdown per design, including the real bugs the process found along the way.

## Why this exists

Verification tooling has a well-known gap: the hardest part of the job isn't running one simulation or one proof, it's connecting specs, RTL, verification environments, coverage, and debugging into a coherent workflow across a project. VerifyRTL is an exploration of how much of that connective work an LLM-assisted pipeline can close — and, just as importantly, how honestly it can report the parts it *can't* close. A recurring theme throughout this project is refusing to let a tool limitation quietly become a false PASS.

## What it does

| Capability | Description |
|---|---|
| **Directed simulation** | Parses RTL, infers ports/clock/reset/FSM, generates a self-checking testbench (golden-model comparison where derivable, monitor-only with an explicit **UNVERIFIED** flag otherwise — never a green PASS the tool can't back up), runs it, and returns logs + an interactive waveform viewer. |
| **Formal verification** | Real SymbiYosys backend: BMC, unbounded PDR, and k-induction, with an automatic **engine/solver fallback chain** (see below) and honest `PROVEN` / `FALSIFIED` / `REACHED` / `UNREACHED` verdicts that are never conflated with `TIMEOUT` / `UNKNOWN` / `ERROR`. |
| **LLM-assisted property engine** | Proposes candidate assert/assume/cover properties from RTL structure and an optional spec, then converts each to SVA — and explicitly **declines** properties that are genuinely multi-cycle rather than silently approximating them as same-cycle claims. |
| **Coverage** | Post-pass code coverage (statement/branch/toggle/FSM) computed from VCD timelines, with an agentic closure loop that proposes new stimulus for uncovered targets. |
| **Spec traceability** | Maps written requirements to verification-plan test categories, surfacing gaps. |
| **Failure triage** | Chat-style Q&A over a failing waveform/trace to help localize root cause. |

## Formal verification: engines, fallback, and honesty

The formal backend doesn't just run one solver and report what it says. `recommended_engine_chain()` (`src/rtl_verify/formal_props.py`) walks an ordered list of genuinely different proof strategies — confirmed against SymbiYosys's own source, not guessed from `--help` text:

- **Sequential assert/assume:** PDR → k-induction (yices) → k-induction (z3). PDR and k-induction are different algorithms with different failure modes; an induction-step failure alone (base case still holding) correctly leaves the verdict unresolved rather than reporting a false counterexample, so the chain only advances past genuinely inconclusive results, never past a real `PASS`/`FAIL`.
- **Cover (reachability):** the same bound, three different SMT solvers — recovers from a solver-specific error without changing what's being searched for.
- **Combinational:** a single BMC step is already exhaustive (SAT-complete), so there's no algorithm to fall back to.

The wall-clock budget the caller sets is **split across the chain's rungs, never silently multiplied** — a documented `timeout_sec` stays a real budget. Every attempt is reported back, so an inconclusive final verdict is never opaque about what was actually tried.

`TIMEOUT` / `UNKNOWN` / `CANCELLED` are surfaced as their own honest verdicts, distinct from both a real proof and a real counterexample — confirmed against SymbiYosys's source that `UNKNOWN` is a real, reachable PDR outcome, not a hypothetical edge case.

**PROVEN isn't the last word, either.** A property can be technically PROVEN for a hollow reason — its own triggering condition never actually happens, so the "proof" says nothing about whatever it was meant to guarantee (`assert(a -> b)` where `a` is never true is trivially true and useless). Every PROVEN assert now gets an automatic **vacuity check** (`src/rtl_verify/vacuity.py`), not just the LLM-suggested properties that happen to carry a hand-written paired cover: it extracts the guard from this project's standard `!(guard) || (conclusion)` property shape and runs a real `cover(guard)` through the solver. The result — `NON_VACUOUS`, `VACUOUS`, `UNKNOWN`, or `NOT_APPLICABLE` (property isn't in that shape) — travels alongside every PROVEN verdict as a `confidence` field, so "PROVEN" is never shown without also saying how much that proof is actually worth.

## Stress-testing against increasing complexity

Rather than validating this pipeline only against toy designs, it's been deliberately run against a sequence of harder RTL — each one chosen to test a *different* axis of difficulty, with results reported honestly, including the failures:

| Design | What it tested | Real finding |
|---|---|---|
| `sync_fifo.v` | Pointer arithmetic, simple control | All same-cycle invariants proven; multi-cycle ordering/overflow claims correctly declined, not approximated. |
| `updown_counter.v` | Saturating arithmetic + control-flow priority | Suggested "no wraparound" properties sometimes came back *tautologically true by bit width alone* (e.g. `count <= 8'hFF` on an 8-bit reg) — a real vacuity gap distinct from the paired-cover check. |
| `divider8.v` | Genuinely iterative sequential math | Two `FALSIFIED` verdicts turned out to be property-phrasing artifacts (comparing against a live input port instead of an internally-latched value) — confirmed by pulling the actual PDR counterexample trace before trusting either as a real bug. |
| `multiplier32.v` | Solver difficulty at width, independent of the LLM pipeline | Proved in ~1s even at 32×32 — an honest negative result, not a forced timeout. |
| `direct_cache.v` | Tag/valid arrays + an external, unconstrained memory interface | Originally only 1 of 11 suggested properties was expressible — split into two distinct limitation categories: temporal/liveness claims (a real, still-open same-cycle ceiling) and internal-signal references the wrapper didn't expose (tags, valid bits, FSM state — invisible since they're not ports). The second category is now closed: `dut_probe.py` instruments a generated *copy* of the RTL (the file on disk is never touched) with debug ports reading every internal register, wired in automatically for every formal run. Re-run against the same design: 11 of 29 proposed properties now expressible (up from 1 of 11) — the remaining declines are genuinely multi-cycle, correctly declined, not a regression. |
| `rv32i_core.v` | A real RV32I core with the actual [RVFI](https://github.com/YosysHQ/riscv-formal) interface | Functional simulation caught two real RTL bugs before any formal run: a classic Verilog gotcha where a ternary's branch signedness silently overrides an explicit `$signed()` cast (root-caused against IEEE 1800-2023 §11.8.1 — see [`docs/systemverilog_ieee1800_rules.md`](docs/systemverilog_ieee1800_rules.md)), and a missing operand-select case for branch instructions. 63 hand-written RVFI properties (following riscv-formal's own check pattern) then covered the full RV32I base ISA — ALU-reg, ALU-imm, LUI/AUIPC, all 6 branches, JAL/JALR, every load/store width/sign combination, and FENCE/SYSTEM trap behavior — and all **63/63 are PROVEN**, per the formal [verification plan](docs/verification_plan_rv32i.md). Getting there surfaced three more real bugs, none in the RTL: the same §11.8.1 signedness rule recurring through the equality operator instead of a ternary; branch/jump `pc_wdata` properties that didn't account for the core's correct trap-on-misaligned-target behavior; and a yosys frontend limitation where bit-selecting a parenthesized sub-expression (`(a + b)[1:0]`) is a syntax error, not a semantic one — fixed by expressing byte offsets and sign-extension with masks (`&`) instead of part-selects. |

Every one of these designs was independently verified against real simulation (Icarus) before any formal claim was trusted against it — several of the "findings" above turned out, on inspection, to be testbench bugs rather than RTL bugs, and are reported as such rather than glossed over.

Adding internal-signal visibility touches shared pipeline code (`analyzer.py`, the formal wrapper path), so after it shipped, every prior sign-off was re-run end to end rather than assumed safe: RV32I's 63/63 RVFI properties and MESI's 25/25 safety/correctness properties + 8/8 coverage goals all re-confirmed with zero regressions.

## Formal deep-dive: MESI cache coherence protocol

A separate, hand-built formal project (`examples/mesi_multi_cache.v` + `examples/mesi_cache_core.v`, `MESI_Formal/`) — SymbiYosys and hand-written SVA directly, not routed through VerifyRTL's LLM-assisted pipeline above, since proving cross-cache coherence invariants needs a genuinely multi-instance formal model (N=3 caches sharing one line over a snooping bus), not a single-DUT wrapper. Full test plan through sign-off: [`docs/verification_plan_mesi.md`](docs/verification_plan_mesi.md).

**Proved by k-induction (unbounded, not a bounded sample):** mutual exclusion of the Modified state across all 3 caches; the general single-writer invariant (E/M implies sole ownership — no stale-Shared cache can coexist with a Modified one); correct read-miss classification (Exclusive vs. Shared depending on whether another cache already holds the line); unconditional invalidation on a remote write; and the "silent upgrade" (E→M with no bus transaction) optimization. 25/25 safety and functional-correctness properties PROVEN; 8/8 coverage goals reached, including a full ownership-transfer transaction confirmed by hand from the raw VCD trace, not just the solver's summary label.

Two real yosys-frontend limitations were found and worked around while building this: bare concurrent SVA (`assert property (@(posedge clk) ...)`) isn't accepted by this project's yosys build at all (confirmed with an isolated probe, independent of this design) — substituted with `$past()`-based immediate assertions; and a static assert/cover label repeated across `generate for` iterations collides on cell naming — per-instance checks are left unlabeled, with SymbiYosys still identifying the failing cache via its hierarchical instance path. Both are documented in the plan, not silently patched around.

## Quick start

```powershell
pip install -r requirements.txt

# CLI
python run_verify.py examples\adder_2bit.v -l systemverilog -o build\adder
python run_verify.py examples\sync_fifo.v --vplan

# Web UI
python -m uvicorn api.main:app --reload --app-dir .
```

Open `http://127.0.0.1:8000` (add `--port` if 8000 is busy). For the LLM-assisted property engine, copy `.env.example` to `.env` and add an `ANTHROPIC_API_KEY` — every other capability works without one.

### Requirements

- Python 3.10+
- [Icarus Verilog](https://bleyer.org/icarus/) for simulation
- [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build) (SymbiYosys + Yosys) for formal verification
- An Anthropic API key for the LLM-assisted property suggestion/conversion engine (optional)

## Architecture

```
RTL upload → analyzer.py (ports, clock/reset, FSM, is_sequential)
          ├─ simulation path:
          │    combinational_model.py / rtl_interpreter.py (golden model where derivable)
          │    → generators/ (verilog | sv | uvm) → backends/ (pluggable simulators)
          │    → waveform.py (VCD → JSON) → coverage.py
          └─ formal path:
               property_suggester.py + property_to_sva.py (LLM, optional)
               → formal_props.py (engine/solver chain selection)
               → backends/symbiyosys.py (SymbiYosys: BMC / PDR / k-induction)
```

Both paths share the same analyzer and the same pluggable backend registry (`backends/registry.py`) — new simulators or solvers plug in as small modules, not pipeline changes.

## Examples

| File | Category |
|---|---|
| `adder_2bit.v`, `and_2bit.v`, `alu_4bit.v`, `alu_8bit.v`, `mux_4to1.v`, `full_adder_4bit.v` | Combinational primitives |
| `traffic_light_fsm.v`, `free_running_counter.v` | Simple sequential / FSM |
| `sync_fifo.v` | Pointer arithmetic + control |
| `updown_counter.v` | Saturating arithmetic + control FSM |
| `divider8.v`, `multiplier32.v` | Complex math (iterative and wide-combinational) |
| `direct_cache.v` | Tag/valid arrays + external memory interface |
| `rv32i_core.v` | Full RV32I core with a real RVFI interface — 63/63 properties PROVEN ([verification plan](docs/verification_plan_rv32i.md)) |
| `mesi_line.v`, `mesi_cache_core.v`, `mesi_multi_cache.v` | MESI cache coherence protocol, single- and multi-cache — 25/25 properties PROVEN, 8/8 covers reached ([verification plan](docs/verification_plan_mesi.md)) |

## Limitations

- Property conversion is **same-cycle only** — genuinely multi-cycle or liveness claims are honestly declined, not approximated. This is a deliberate design choice, not an oversight, and its boundary is exactly what the complexity-validation designs above are chosen to probe.
- Self-checking simulation depends on a derivable golden model; otherwise runs are monitor-only and flagged `UNVERIFIED`.
- FSM path coverage is directed, not formal.
- Internal-signal visibility (`dut_probe.py`) is capped at 24 registers per design by default (override by naming specific signals) and, like the rest of this pipeline, is same-cycle only — an internal array's debug select index is a free input, so a property comparing it *across* two cycles must explicitly pin the index stable itself (see `property_suggester.py`'s prompt) or it will silently compare two different array entries.

## Roadmap

- cocotb + Verilator backend
- FSM transition golden models and coverage

---

Built as a research project into how far an LLM-assisted pipeline can close the gap between spec, RTL, and verification sign-off — and how honestly it can report where it can't.
