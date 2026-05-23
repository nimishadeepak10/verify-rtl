# RTL Verification Automation Tool

Automates a **directed-test** RTL verification flow: parse your design, generate a Verilog/SystemVerilog testbench (or UVM skeleton), run **Icarus Verilog**, and return **text logs** plus **VCD waveform** data.

## What it does

| Step | Description |
|------|-------------|
| 1. Upload RTL | e.g. `examples/adder_2bit.v` |
| 2. Analyze | Extract module ports; infer simple ops (`+`, `&`, `^`) for self-checking |
| 3. Generate TB | Exhaustive tests for small port widths; random for larger |
| 4. Simulate | `iverilog` + `vvp` → `sim.vcd` |
| 5. Report | PASS/FAIL in log, full text report, waveform samples as text, VCD download |

## Requirements

- **Python 3.10+**
- **[Icarus Verilog](https://bleyer.org/icarus/)** (`iverilog`, `vvp`) for simulation
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
```

### Web UI

```powershell
pip install -r requirements.txt
python -m uvicorn api.main:app --reload --app-dir c:\Users\Nimisha\verification_tb
```

Open http://127.0.0.1:8000

- **Left:** upload or paste RTL, choose Verilog / SystemVerilog / UVM, run verification
- **Right sidebar:** live **test plan preview** (ports, planned test count, checklist) before you click Run

## Languages

- **verilog** / **systemverilog**: Full flow (TB + sim + VCD + text waveform)
- **uvm**: Sequence/driver/monitor/env skeleton; use your corporate UVM simulator separately

## Architecture

```
RTL upload → analyzer.py (ports, inferred op)
          → generators/ (verilog | sv | uvm)
          → simulator.py (iverilog)
          → waveform.py (VCD → text)
          → report.txt + sim.vcd
```

## Limitations (MVP)

- Self-checking golden model only for inferred combinational ops (add/and/xor)
- No clock/reset protocol generation for sequential designs yet
- No formal, coverage, or constrained-random UVM scoreboarding
- Complex SystemVerilog RTL may need manual top module name

## Roadmap ideas

- LLM-assisted TB for sequential FSMs
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
