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

## Stress-testing against increasing complexity

Rather than validating this pipeline only against toy designs, it's been deliberately run against a sequence of harder RTL — each one chosen to test a *different* axis of difficulty, with results reported honestly, including the failures:

| Design | What it tested | Real finding |
|---|---|---|
| `sync_fifo.v` | Pointer arithmetic, simple control | All same-cycle invariants proven; multi-cycle ordering/overflow claims correctly declined, not approximated. |
| `updown_counter.v` | Saturating arithmetic + control-flow priority | Suggested "no wraparound" properties sometimes came back *tautologically true by bit width alone* (e.g. `count <= 8'hFF` on an 8-bit reg) — a real vacuity gap distinct from the paired-cover check. |
| `divider8.v` | Genuinely iterative sequential math | Two `FALSIFIED` verdicts turned out to be property-phrasing artifacts (comparing against a live input port instead of an internally-latched value) — confirmed by pulling the actual PDR counterexample trace before trusting either as a real bug. |
| `multiplier32.v` | Solver difficulty at width, independent of the LLM pipeline | Proved in ~1s even at 32×32 — an honest negative result, not a forced timeout. |
| `direct_cache.v` | Tag/valid arrays + an external, unconstrained memory interface | Only 1 of 11 suggested properties was expressible — the clearest demonstration yet of the same-cycle-only ceiling, cleanly split into two distinct limitation categories (temporal/liveness claims vs. internal-signal references the wrapper doesn't expose). |
| `rv32i_core.v` *(in progress)* | A real RV32I core with the actual [RVFI](https://github.com/YosysHQ/riscv-formal) interface | Functional simulation caught two real RTL bugs before any formal run: a classic Verilog gotcha where a ternary's branch signedness silently overrides an explicit `$signed()` cast, and a missing operand-select case for branch instructions. RVFI-based per-instruction correctness properties (hand-written, following riscv-formal's own check pattern) are now proving out too — 25/25 for the ALU-reg, ALU-imm, and LUI/AUIPC groups so far, including two more real bugs found in the *properties themselves* (a missing RVFI write-suppression invariant, and a yosys formal-frontend limitation with `$signed(...) >>> ...`). Branches, jumps, and loads/stores are the remaining in-progress work. |

Every one of these designs was independently verified against real simulation (Icarus) before any formal claim was trusted against it — several of the "findings" above turned out, on inspection, to be testbench bugs rather than RTL bugs, and are reported as such rather than glossed over.

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
| `rv32i_core.v` | Full RV32I core with a real RVFI interface *(in progress)* |

## Limitations

- Property conversion is **same-cycle only** — genuinely multi-cycle or liveness claims are honestly declined, not approximated. This is a deliberate design choice, not an oversight, and its boundary is exactly what the complexity-validation designs above are chosen to probe.
- Self-checking simulation depends on a derivable golden model; otherwise runs are monitor-only and flagged `UNVERIFIED`.
- FSM path coverage is directed, not formal.
- The property-suggestion wrapper exposes DUT ports only, not internal registers — see the `direct_cache.v` finding above.

## Roadmap

- Close the internal-signal-exposure gap found in the cache stage (expose selected internal registers to the formal wrapper)
- Finish the RVFI-based per-instruction correctness checks for `rv32i_core.v`
- cocotb + Verilator backend
- FSM transition golden models and coverage

---

Built as a research project into how far an LLM-assisted pipeline can close the gap between spec, RTL, and verification sign-off — and how honestly it can report where it can't.
