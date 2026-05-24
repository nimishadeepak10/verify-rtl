# RTL Verification Automation Tool

Automates a **directed-test** RTL verification flow: parse your design, generate a Verilog/SystemVerilog testbench (or UVM skeleton), run **Icarus Verilog**, and return **text logs** plus **VCD waveform** data.

## What it does

| Step | Description |
|------|-------------|
| 1. Upload RTL | e.g. `examples/adder_2bit.v` or `examples/traffic_light_fsm.v` |
| 2. Analyze | Extract ports; infer combinational ops; detect **clock**, **reset**, **FSM** for sequential designs |
| 3. Generate TB | Combinational: exhaustive/random data tests. Sequential: clock + reset sequence, then clocked data stimulus |
| 4. Simulate | Pluggable backend (`icarus`, `reference`, …) → `sim.vcd` |
| 5. Report | PASS/FAIL in log, full text report, waveform samples as text, VCD download |

## Sequential designs

The analyzer (`analyzer.py`) classifies clocked RTL and drives sequential testbench generation:

| Detection | Names / rules |
|-----------|----------------|
| Clock port | `clk`, `clock`, `ck` (case-insensitive input ports) |
| Reset port | `rst`, `reset` (active-high); `rst_n`, `resetn`, `nrst`, `*_n` (active-low) |
| Sequential | `always @(posedge/negedge …)`, `always_ff`, or non-blocking `<=` |
| FSM | `state_reg` assigned in a clocked `always` block and used in a `case` — state labels extracted from `case` branches |

Generated sequential testbenches toggle the clock, apply reset with correct polarity, then apply stimulus only on **data inputs** (clock/reset excluded) with `@(posedge clk)` synchronization.

See `examples/traffic_light_fsm.v` for a minimal Moore FSM with `clk`, active-low `rst_n`, and a `go` input.

## Requirements

- **Python 3.10+**
- **[Icarus Verilog](https://bleyer.org/icarus/)** (`iverilog`, `vvp`) for simulation (required for sequential designs)
- **UVM**: generated only; run with Questa/VCS/Xcelium (not supported by Icarus)

### Windows install (Icarus)

```powershell
# Example: via installer from https://bleyer.org/icarus/ or chocolatey
choco install icarus-verilog
```

## Quick start

```powershell
cd c:\Users\Nimisha\verification_tb
pip install -r requirements.txt
python run_verify.py examples\adder_2bit.v -l systemverilog -o build\adder
python run_verify.py examples\traffic_light_fsm.v -l systemverilog -o build\fsm
```

### Web UI

```powershell
pip install -r requirements.txt
python -m uvicorn api.main:app --reload --app-dir c:\Users\Nimisha\verification_tb
```

Open http://127.0.0.1:8000 (or another port if 8000 is in use).

**Workspace UI (Phase A.7):** a five-step flow — **Design → Plan → Run → Results → Coverage** — with left navigation, instrument-panel styling (JetBrains Mono + IBM Plex Sans, amber trace accent), and a full **verification plan** on the Plan screen (expandable categories, toggles, coverage goals). Load `examples/adder_2bit.v` via **Load example**, analyze, then run verification end-to-end.

## Languages

- **verilog** / **systemverilog**: Full flow (TB + sim + VCD + text waveform)
- **uvm**: Sequence/driver/monitor/env skeleton; use your corporate UVM simulator separately

## Architecture

```
RTL upload → analyzer.py (ports, clock/reset, FSM, inferred op)
          → generators/ (verilog | sv | uvm)
          → backends/ (pluggable simulators via registry)
          → waveform.py (VCD → text)
          → report.txt + sim.vcd
```

Simulation is handled by a **pluggable backend** layer (`src/rtl_verify/backends/`). Each backend implements `SimulatorBackend` with `is_available()`, `version()`, and `run()`. The registry (`registry.py`) lists backends in priority order—Icarus first, then the built-in Python reference for combinational RTL—and `auto_select()` picks the first available match. New tools (Vivado XSim, Verilator, cocotb) can be added as small plugins without changing the pipeline. Use `GET /api/backends` or the web UI simulator dropdown to inspect or override the choice.

## Verification Plan

Structured verification plans (`vplan`) model industry-style test planning **before** testbench generation. The data lives in `src/rtl_verify/vplan.py`:

| Type | Role |
|------|------|
| `VerificationPlan` | Top-level document: DUT overview, strategy, categories, coverage goals, pass/fail criteria, notes |
| `TestCategory` | Directed, corner, negative, random, exhaustive — each with status (`enabled` / `disabled` / `n/a`) |
| `TestSubcategory` | e.g. walking-one, X-propagation, overflow — with explicit N/A reasons |
| `TestCase` | Concrete vectors: `inputs`, optional `expected_outputs`, per-case `rationale` |
| `CoverageGoal` | Statement/toggle/FSM/functional targets with rationale |

`vplan_builder.build_vplan()` analyzes the DUT and fills categories automatically (e.g. exhaustive N/A when input space > 512; overflow N/A when the adder output is wide enough). User toggles via `enabled_categories` / `enabled_subcategories` cannot force-enable N/A items — a warning note is added instead.

**CLI:** `python run_verify.py examples\adder_2bit.v --vplan`  
**API:** `POST /api/vplan` (JSON body via form fields, same toggles as above)

The existing `/api/analyze` preview and web UI are unchanged in Part 1; Part 2 will render the vplan in the UI and drive the testbench from it.

## Limitations

- Self-checking golden model only for inferred combinational ops (add/and/xor)
- Sequential designs use monitor-style checks unless a combinational golden model applies
- FSM path coverage is directed (exhaustive on data inputs), not formal
- Complex SystemVerilog RTL may need manual top module name

## Roadmap ideas

- FSM transition golden models and coverage
- cocotb + Verilator backend
- GTKWave PNG export or Surfer WASM in the browser
- Questa/VCS runner plugins for production UVM

## Example output (2-bit adder)

```
PASS a=0 b=0 sum=0
...
RESULT: PASS
=== WAVEFORM (text) ===
t=5ns  a=0  b=0  sum=0
...
```

Download `sim.vcd` from the web UI or `build/adder/sim.vcd` after CLI run.
