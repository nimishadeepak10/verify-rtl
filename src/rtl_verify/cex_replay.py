"""Counterexample replay: is a counterexample found under an abstraction a
real one?

A cut point, a black box or a counter abstraction only ADD behaviours, so a
PROVEN under one holds for the real design, but a FALSIFIED may be an artifact
of a behaviour the real design cannot show. The standard answer in model
checking (counterexample validation in CEGAR) is to run the counterexample's
inputs on the real design and see whether the property really breaks. That is
cheap: one short simulation, instead of the full proof the abstraction was
there to avoid.

What a replay can and cannot say:
  CONFIRMED         the property is violated when the trace's inputs are driven
                    into the real design. Any violation of an asserted
                    property under inputs that satisfied the assumptions is a
                    real counterexample, so this is a real bug.
  NOT_REPRODUCED    the property held for the whole replay. That is NOT proof
                    the counterexample was spurious: a counter abstraction's
                    trace is short because the counter was allowed to jump,
                    and the real counter has not got there yet. The caller
                    must fall back to a proof on the real design.
  UNAVAILABLE       the replay could not be built or run (no simulator, a
                    construct the simulator rejects, no usable trace).

Only the DUT's INPUT values are taken from the trace; internal state is
whatever the real design computes, which is the point.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .analyzer import PortDirection, RtlModule


@dataclass
class ReplayResult:
    status: str                 # CONFIRMED | NOT_REPRODUCED | UNAVAILABLE
    violated_at_step: Optional[int] = None
    steps: int = 0
    detail: str = ""


def _step_times(wf: dict) -> List[int]:
    by_base = {s["name"].split(".")[-1]: s for s in wf["signals"]}
    step = by_base.get("smt_step")
    if step is not None:
        return sorted({t["time"] for t in step["transitions"]})
    return sorted({t["time"] for s in wf["signals"] for t in s["transitions"]})


def _value_at(sig: dict, time: int) -> str:
    v = sig["transitions"][0]["value"]
    for t in sig["transitions"]:
        if t["time"] <= time:
            v = t["value"]
    return v


def _literal(bits: str, width: int) -> str:
    bits = re.sub(r"[^01]", "0", bits).zfill(width)[-width:]
    return f"{width}'b{bits}"


def _split_past(expr: str) -> Tuple[str, List[str]]:
    """Replace each `$past(<inner>)` with a testbench register name; return the
    rewritten expression and the inner expressions (single-argument only)."""
    out, inners, i = [], [], 0
    while i < len(expr):
        if expr.startswith("$past(", i):
            j, depth = i + len("$past("), 1
            while j < len(expr) and depth:
                depth += expr[j] == "("
                depth -= expr[j] == ")"
                j += 1
            inner = expr[i + len("$past("): j - 1]
            if "," in inner and inner.count("(") == inner.count(")") and re.search(r",\s*\d+\s*$", inner):
                raise ValueError("$past with a depth argument is not supported by the replay")
            out.append(f"__past_{len(inners)}")
            inners.append(inner)
            i = j
        else:
            out.append(expr[i])
            i += 1
    return "".join(out), inners


def build_replay_testbench(module: RtlModule, expr: str, wf: dict) -> Tuple[str, int]:
    """Testbench that drives the trace's inputs into the DUT and reports each
    step at which `expr` is false. Returns (text, number of steps)."""
    times = _step_times(wf)
    by_base = {s["name"].split(".")[-1]: s for s in wf["signals"]}
    clk, rst = module.clock_port, module.reset_port
    ins = [p for p in module.ports if p.direction == PortDirection.INPUT]
    outs = [p for p in module.ports if p.direction != PortDirection.INPUT]
    rewritten, pasts = _split_past(expr)

    decls = []
    for p in ins:
        rng = p.range_str()
        decls.append(f"    reg {rng + ' ' if rng else ''}{p.name};")
    for p in outs:
        rng = p.range_str()
        decls.append(f"    wire {rng + ' ' if rng else ''}{p.name};")
    for k in range(len(pasts)):
        decls.append(f"    reg [63:0] __past_{k};")
    conns = ", ".join(f".{p.name}({p.name})" for p in module.ports)

    inactive = ""
    if rst:
        inactive = f"({rst} === 1'b1)" if module.reset_active_low else f"({rst} === 1'b0)"
    body: List[str] = []
    for k, t in enumerate(times):
        for p in ins:
            if p.name == clk:
                continue
            sig = by_base.get(p.name)
            val = _value_at(sig, t) if sig is not None else "0"
            body.append(f"        {p.name} = {_literal(val, p.width)};")
        body.append("        #1;")
        guard = f"({k} > 0)" if pasts else "1"
        if pasts and rst:
            guard += f" && {inactive} && __prev_ok"
        body.append(f"        if ({guard}) if (!({rewritten})) $display(\"VIOL %0d\", {k});")
        for n, inner in enumerate(pasts):
            body.append(f"        __past_{n} = ({inner});")
        if rst:
            body.append(f"        __prev_ok = {inactive};")
        if clk:
            body.append(f"        {clk} = 1; #5; {clk} = 0; #4;")
        else:
            body.append("        #9;")
    init = [f"        {p.name} = 0;" for p in ins]
    text = ("`timescale 1ns/1ps\nmodule __replay_tb;\n" + "\n".join(decls)
            + f"\n    reg __prev_ok;\n    {module.name} dut ({conns});\n    initial begin\n"
            + "        __prev_ok = 1'b0;\n" + "\n".join(init) + "\n" + "\n".join(body)
            + "\n        $display(\"DONE\");\n        $finish;\n    end\nendmodule\n")
    return text, len(times)


def replay_counterexample(module: RtlModule, rtl_source: str, expr: str, waveform_json: Optional[dict],
                          work_dir: Optional[Path] = None, timeout_sec: int = 60) -> ReplayResult:
    """`module`/`rtl_source` are the PROBED design (so `__dbg_*` names in
    `expr` resolve), exactly as the formal run saw it."""
    from .backends.icarus import find_iverilog, find_vvp

    if not waveform_json or "signals" not in waveform_json:
        return ReplayResult("UNAVAILABLE", detail="no counterexample trace to replay")
    iverilog = find_iverilog()
    vvp = find_vvp(iverilog) if iverilog else None
    if not (iverilog and vvp):
        return ReplayResult("UNAVAILABLE", detail="Icarus Verilog not found")
    try:
        tb, steps = build_replay_testbench(module, expr, waveform_json)
    except ValueError as e:
        return ReplayResult("UNAVAILABLE", detail=str(e))
    if steps == 0:
        return ReplayResult("UNAVAILABLE", detail="the trace has no steps")
    work = work_dir or Path(tempfile.mkdtemp(prefix="replay_"))
    work.mkdir(parents=True, exist_ok=True)
    (work / "dut.sv").write_text(rtl_source, encoding="utf-8")
    (work / "tb.sv").write_text(tb, encoding="utf-8")
    exe = work / "replay.vvp"
    comp = subprocess.run([iverilog, "-g2012", "-o", str(exe), str(work / "dut.sv"), str(work / "tb.sv")],
                          capture_output=True, text=True, timeout=timeout_sec)
    if comp.returncode != 0:
        first = (comp.stderr or comp.stdout).strip().splitlines()[:1]
        return ReplayResult("UNAVAILABLE", steps=steps,
                            detail="the simulator rejected the replay: " + (first[0][:140] if first else ""))
    try:
        run = subprocess.run([vvp, str(exe)], capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return ReplayResult("UNAVAILABLE", steps=steps, detail="the replay timed out")
    viol = [int(m.group(1)) for m in re.finditer(r"VIOL (\d+)", run.stdout)]
    if viol:
        return ReplayResult("CONFIRMED", violated_at_step=min(viol), steps=steps,
                            detail="the property is violated on the real design under the trace's inputs")
    if "DONE" not in run.stdout:
        return ReplayResult("UNAVAILABLE", steps=steps, detail="the replay did not run to completion")
    return ReplayResult("NOT_REPRODUCED", steps=steps,
                        detail="the property held for the whole replay; this does not by itself show the "
                               "counterexample was spurious")
