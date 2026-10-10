# VerifyRTL

LLM-assisted RTL verification: directed simulation and formal proof in one pipeline, built so it never reports a stronger verdict than a real solver run earned.

It runs on real tools, [Icarus Verilog](https://bleyer.org/icarus/) for simulation and [SymbiYosys](https://github.com/YosysHQ/sby)/[Yosys](https://github.com/YosysHQ/yosys) for formal. An LLM is used only to propose properties, convert claims to SVA, and explain failures. Every verdict comes from a solver or simulator.

Proved 63 of 63 properties on a RISC-V RV32I core and 25 of 25 on a 3-cache MESI coherence model, with simulation catching two real RTL bugs along the way.

**Try it in one command** (needs SymbiYosys; no API key):

```powershell
python scripts\demo.py
```

It plans which techniques a small FIFO needs, runs the proofs, and shows three properties PROVEN and one deliberately wrong one FALSIFIED with a counterexample.

## What you get

- **Simulation:** infers ports, clock, reset and FSM, generates a self-checking testbench, runs it, and shows a waveform. If no golden model can be derived, the run is flagged UNVERIFIED rather than passed.
- **Formal proof:** BMC, unbounded PDR and k-induction with an engine and solver fallback chain. `PROVEN`, `FALSIFIED`, `REACHED` and `UNREACHED` are never mixed up with `TIMEOUT`, `UNKNOWN` or `ERROR`.
- **Properties:** the LLM proposes assert, assume and cover properties from the RTL and an optional spec, then converts them to SVA. Claims that are genuinely multi-cycle are declined, not approximated.
- **Coverage, traceability, triage, CDC scan:** code coverage from VCD, requirement-to-test mapping, chat over a failing trace, and a static clock-domain-crossing scan.

## What makes it different

1. **Honest verdicts.** A timeout is never shown as a proof. A definitive result is re-checked by a second, independent engine.
2. **Proofs are checked for being hollow.** Vacuity of each assert, consistency of the assumptions, which assumptions the proof actually needed, an SVA linter, and signal coverage.
3. **Abstractions say what they mean.** Each technique is labelled: sound for PROVEN, FALSIFIED may be an artifact, or valid only for a reduced configuration. Nothing is applied silently.
4. **Techniques are chosen per design.** A planner profiles the design and the goal, skips what does not apply, and explains why. An executor can then run the plan one technique at a time.
5. **Stuck proofs have a toolkit** (table below), including helper-invariant mining proven by induction before use.
6. **Progress, not just safety.** Deadlock, livelock and starvation are checked as bounded response, with fairness as an explicit assumption.
7. **Assertion search is scored by the solver, not by a model.** A tree search adapted from SANGAM (IEEE ICLAD 2025) refines assertions per signal, rewarding only what a solver proves, non-vacuous and independent.

## The formal toolkit

| Technique | Use it when | What the answer means |
|---|---|---|
| Plain proof (PDR, then k-induction) | Always first | Exact |
| Helper-invariant mining | Induction fails on a true property | Exact |
| ROM to case | A constant lookup table is in the cone | Exact |
| Assertion decomposition | A property is a conjunction | Exact |
| Counter abstraction | A wide counter is compared to large values | PROVEN is real, FALSIFIED may be an artifact |
| Cut points | A wide accumulator feeds a status bit | PROVEN is real, FALSIFIED may be an artifact |
| Black-boxing | A submodule holds big memory or arithmetic | PROVEN is real, FALSIFIED may be an artifact |
| Data-width reduction | Data provably never reaches control | Exact for properties that ignore data |
| Parameter reduction | Hunting bugs at a smaller size | Bounded configuration, not a proof of the real design |
| Case-split completeness | You split a proof by opcode or mode | Proves the cases cover everything |
| Assumption necessity | You supplied assumptions | Which ones the proof needed, and what each excludes |
| Bounded response (progress) | Requests must be answered | Exact at the stated bound |

## Choosing techniques

`POST /api/formal/plan` returns a plan with no solver run: for each technique a decision (`RECOMMENDED`, `IF_STUCK`, `NOT_NEEDED`, `NOT_APPLICABLE`, `NOT_ACCEPTABLE_FOR_GOAL`), the reason, the evidence and the parameters to apply it. Goals are `prove`, `signoff`, `find_bugs`, `progress` and `explore`; the goal decides what an answer is allowed to mean.

`POST /api/formal/plan/execute` runs it: the plain proof plus exact transforms first, then reserve techniques one at a time, each only on the properties still open. Results are labelled by what produced them, and a FALSIFIED under an abstraction comes back as unconfirmed, never as a bug.

## Results so far

| Design | Outcome |
|---|---|
| `rv32i_core.v` (RISC-V core, RVFI) | 63 of 63 properties proven. Simulation caught two real RTL bugs before formal. |
| `mesi_multi_cache.v` (3 caches, snooping bus) | 25 of 25 safety properties proven by k-induction, 8 of 8 covers reached. |
| `sync_fifo.v` | Same-cycle invariants proven; multi-cycle ordering claims declined, not approximated. |
| `multiplier32.v` | Proved in about a second: a negative result reported as is. |
| CDC scanner on real designs | Five scanner bugs found and fixed by moving to real multi-clock RTL. |

Details, including the failures: [docs/full_reference.md](docs/full_reference.md).

## Quick start

```powershell
pip install -r requirements.txt

# CLI
python run_verify.py examples\adder_2bit.v -l systemverilog -o build\adder
python run_verify.py examples\sync_fifo.v --vplan

# Web UI and API
python -m uvicorn api.main:app --reload --app-dir .
```

Open `http://127.0.0.1:8000`. For the LLM features, copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`; everything else works without it.

Requirements: Python 3.10+, Icarus Verilog, and the [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build) (SymbiYosys and Yosys). On Windows, run yosys and sby through PowerShell.

## API

| Endpoint | Purpose |
|---|---|
| `/api/verify` | Simulation run |
| `/api/formal` | Formal check with the options above (cut points, invariants, abstractions, assumption checks) |
| `/api/formal/plan` | Recommend techniques for a design and goal |
| `/api/formal/plan/execute` | Run the plan, one technique at a time |
| `/api/formal/progress` | Bounded response, with deadlock, livelock and starvation labels |
| `/api/formal/assertion_search` | Per-signal assertion search scored by the solver |
| `/api/formal/case_split` | Check a case split covers the input space |
| `/api/formal/suggest`, `/convert` | LLM property suggestion and SVA conversion |
| `/api/formal/mutation_adequacy` | Do the properties catch injected faults |
| `/api/cdc` | Static CDC and RDC scan |
| `/api/coverage`, `/api/traceability`, `/api/chat` | Coverage, requirement mapping, trace Q and A |

## Layout

```
api/main.py            FastAPI endpoints
src/rtl_verify/        analyzer, formal wrapper, backends, and one module per technique
  strategy_planner.py  plan_executor.py   invariant_mining.py   progress_check.py
  assertion_search.py  assumption_necessity.py   cutpoint.py   counter_abstract.py ...
examples/              designs used for testing
scripts/test_*.py      one test script per feature (add --solver for real solver runs)
docs/                  full reference, verification plans, SystemVerilog rules
```

## Limitations

- Property conversion reaches one clock cycle back (`$past`); longer multi-cycle claims are declined.
- Self-checking simulation needs a derivable golden model; otherwise it is monitor-only and flagged.
- The formal path assumes a single clock domain. Multi-clock designs get the static CDC scan, which is a lint, not a proof.
- Technique detection is textual. A structure hidden behind a macro or generate block can be missed, so every skip states its reason and can be overridden.
- A design that instantiates a submodule from another file needs that file concatenated in first.
- Liveness is checked as a bound. A failure at every bound up to a cap is evidence of starvation, not a proof.

## Roadmap

- Pluggable connectors for commercial tools (Xcelium, JasperGold, VCS) and Verilator
- FSM transition golden models and coverage
