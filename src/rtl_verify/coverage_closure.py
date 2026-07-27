"""Agentic coverage-closure loop (Phase 3): read the coverage gaps a run
already left behind, ask an LLM to propose new directed input vectors that
target them, apply those vectors, re-measure coverage, and repeat until a
stop condition is hit — no hand-written directed tests.

Deliberately independent of pipeline.py and generators/*.py (the default
waveform-testbench path) — those have their own in-flight, uncommitted
work this session was told to leave alone. This module builds its own
minimal directed-stimulus testbench and its own thin run loop, reusing
analyzer.py / coverage.py / rtl_interpreter.py / waveform.py / the backend
registry as read-only building blocks, the same way the Phase 1/2 formal
work stayed independent of the simulation pipeline.

Coverage is measured over one continuous VCD trace, so each iteration
reruns the FULL accumulated vector list (baseline + everything proposed so
far), not just the newest vectors — there is no sound way to "merge"
coverage across separate isolated runs without a shared timeline.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from . import llm_client
from .analyzer import RtlModule, analyze_rtl
from .backends.registry import auto_select, get_backend
from .coverage import (
    CoverageReport,
    FsmCoverage,
    build_env_timeline_from_vcd_signals,
    compute_fsm_coverage,
    compute_overall_percent,
    compute_toggle_coverage,
)
from .rtl_features import dut_source_extension
from .rtl_interpreter import CoverageInterpreter
from .waveform import load_module_info, vcd_to_json, write_dut_info

_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = _ROOT / "logs"
LOG_PATH = LOG_DIR / "coverage_closure.jsonl"


def log_event(detail: dict[str, Any]) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), **detail}
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def read_recent(limit: int = 30) -> list[dict[str, Any]]:
    """Most recent logged closure-loop rounds first — survives across
    sessions the same way formal_log.py's history does.
    """
    if not LOG_PATH.is_file():
        return []
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    events.reverse()
    return events


def extract_gaps(cov: CoverageReport) -> dict:
    """Structured gap data — what a run did NOT exercise — as data for an
    LLM prompt, not report text. p3-1.
    """
    missed_branches = [b for b in (cov.branch.branches or []) if not b.get("hit")]
    partial_toggle: list[dict] = []
    for sig, meta in (cov.toggle.per_signal or {}).items():
        both = meta.get("bits_both") or []
        untoggled = [i for i, hit in enumerate(both) if not hit]
        if untoggled:
            partial_toggle.append({
                "signal": sig,
                "width": meta.get("width"),
                "untoggled_bit_indices": untoggled,
            })
    fsm = cov.fsm
    return {
        "overall_percent": cov.overall_percent,
        "uncovered_statement_lines": list(cov.statement.uncovered_lines or []),
        "missed_branches": missed_branches,
        "partial_toggle_signals": partial_toggle,
        "fsm_states_missed": [] if fsm.not_applicable else list(fsm.states_missed or []),
        "fsm_transitions_missed": (
            [] if fsm.not_applicable else [list(t) for t in (fsm.transitions_missed or [])]
        ),
    }


VECTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "vectors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "inputs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "integer"},
                            },
                            "required": ["name", "value"],
                            "additionalProperties": False,
                        },
                    },
                    "hold_cycles": {"type": "integer"},
                    "rationale": {"type": "string"},
                },
                "required": ["inputs", "hold_cycles", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["vectors"],
    "additionalProperties": False,
}

_SYSTEM = """You are proposing directed input stimulus vectors to close code-coverage gaps on a \
digital design, given its RTL and (if this isn't the first round) a structured list of exactly \
what a previous simulation run failed to exercise.

Rules:
- Every "name" in "inputs" must be one of the DUT's own data input ports, given below, spelled \
exactly — never the clock or reset port, and never a signal that doesn't exist.
- Every "value" must fit the named port's bit width (given below) — do not propose a value that \
doesn't fit.
- "hold_cycles" is how many clock edges to hold this vector before the next one (ignored for \
combinational designs — use 1).
- If a gap list is given, prioritize vectors that plausibly hit those specific uncovered lines/
branches/toggle-bits/FSM states/transitions — explain which gap each vector targets in \
"rationale". Do not propose vectors unrelated to the actual gaps.
- If no gap list is given (first round), propose a reasonable spread of corner-case vectors: \
all-zero, all-ones/max value, and a few boundary/mid-range values per input — the goal is a good \
opening spread, not exhaustive enumeration.
- Propose between 4 and 10 vectors per round.
"""


def build_vector_prompt(module: RtlModule, rtl_source: str, gaps: dict | None) -> str:
    ports = "\n".join(
        f"  - {p.name} ({p.direction.value}, width {p.width})" for p in module.data_inputs
    )
    parts = [
        f"Module: {module.name}",
        f"Sequential: {module.is_sequential}",
        "Data input ports (never propose clock/reset here):",
        ports,
        "",
        "RTL source:",
        "```",
        rtl_source,
        "```",
    ]
    if gaps:
        parts.extend(["", "Coverage gaps from the previous round:", json.dumps(gaps, indent=2)])
    else:
        parts.extend(["", "(First round — no coverage data yet.)"])
    return "\n".join(parts)


def propose_vectors(module: RtlModule, rtl_source: str, gaps: dict | None = None) -> list[dict]:
    """Return a list of {"inputs": [{"name","value"}], "hold_cycles", "rationale"}. p3-2."""
    user = build_vector_prompt(module, rtl_source, gaps)
    result = llm_client.complete_structured(_SYSTEM, user, VECTOR_SCHEMA, max_tokens=3000)
    return result.get("vectors", [])


def _dut_port_connections(mod: RtlModule) -> str:
    decls = []
    for p in mod.ports:
        r = p.range_str()
        kind = "reg" if p.direction.value == "input" else "wire"
        decls.append(f"    {kind} {r} {p.name};")
    return "\n".join(decls)


def _instance_ports(mod: RtlModule) -> str:
    return ",\n".join(f"        .{p.name}({p.name})" for p in mod.ports)


def _reset_sequence(mod: RtlModule, clk: str) -> str:
    if not mod.reset_port:
        return f"        repeat(2) @(posedge {clk});\n"
    rst = mod.reset_port
    active, inactive = ("0", "1") if mod.reset_active_low else ("1", "0")
    return (
        f"        {rst} = {active};\n"
        f"        repeat(3) @(posedge {clk});\n"
        f"        {rst} = {inactive};\n"
        f"        repeat(2) @(posedge {clk});\n"
    )


def _vector_assignments(mod: RtlModule, vector: dict) -> str:
    widths = {p.name: p.width for p in mod.data_inputs}
    lines = []
    for entry in vector.get("inputs", []):
        name = str(entry.get("name", ""))
        if name not in widths:
            continue  # never trust an invented/clock/reset signal name
        width = widths[name]
        try:
            value = int(entry.get("value", 0)) & ((1 << width) - 1)
        except (TypeError, ValueError):
            continue
        lines.append(f"        {name} = {value};")
    return "\n".join(lines)


def generate_directed_testbench(mod: RtlModule, vectors: list[dict]) -> str:
    """A minimal waveform-only testbench driven by explicit directed
    vectors instead of the default formulaic stimulus pattern.
    """
    if not mod.outputs:
        raise ValueError("Need at least one output port to generate a testbench")

    stim_lines: list[str] = []
    clk = mod.clock_port
    for i, vec in enumerate(vectors):
        rationale = str(vec.get("rationale", "")).replace("\n", " ")[:120]
        stim_lines.append(f"        // vector {i}: {rationale}")
        stim_lines.append(_vector_assignments(mod, vec))
        if mod.is_sequential and clk:
            hold = max(1, int(vec.get("hold_cycles", 1) or 1))
            stim_lines.append(f"        repeat({hold}) @(posedge {clk});")
        else:
            stim_lines.append("        #10;")
    stim = "\n".join(stim_lines) if stim_lines else "        #20;\n"

    default_init = "\n".join(f"        {p.name} = 0;" for p in mod.data_inputs)

    if mod.is_sequential and clk:
        clock_block = (
            f"    initial {clk} = 0;\n    always #5 {clk} = ~{clk};\n"
            if clk in {p.name for p in mod.ports}
            else f"    reg {clk};\n    initial {clk} = 0;\n    always #5 {clk} = ~{clk};\n"
        )
        reset_block = _reset_sequence(mod, clk)
        return f"""`timescale 1ns/1ps
// Auto-generated coverage-closure testbench for {mod.name} (sequential, directed vectors)
module tb_{mod.name};
{_dut_port_connections(mod)}

    {mod.name} uut (
{_instance_ports(mod)}
    );

{clock_block}
    initial begin
        $dumpfile("sim.vcd");
        $dumpvars(0, tb_{mod.name});
    end

    initial begin
{default_init}
{reset_block}
{stim}
        repeat(5) @(posedge {clk});
        $display("RESULT: DONE");
        $finish;
    end
endmodule
"""
    return f"""`timescale 1ns/1ps
// Auto-generated coverage-closure testbench for {mod.name} (combinational, directed vectors)
module tb_{mod.name};
{_dut_port_connections(mod)}

    {mod.name} uut (
{_instance_ports(mod)}
    );

    initial begin
        $dumpfile("sim.vcd");
        $dumpvars(0, tb_{mod.name});
    end

    initial begin
{default_init}
{stim}
        #20;
        $display("RESULT: DONE");
        $finish;
    end
endmodule
"""


def _run_and_measure(
    mod: RtlModule, rtl_source: str, vectors: list[dict], work: Path, backend_name: str | None,
) -> tuple[CoverageReport | None, Path | None, str, str]:
    """Generate the directed TB, run it, and compute a fresh CoverageReport.
    Returns (coverage_or_none, vcd_path_or_none, sim_log, testbench_source).
    """
    tb_source = generate_directed_testbench(mod, vectors)
    dut_ext = dut_source_extension(rtl_source, "systemverilog")
    rtl_path = work / f"dut{dut_ext}"
    tb_path = work / "tb.sv"
    rtl_path.write_text(rtl_source, encoding="utf-8")
    tb_path.write_text(tb_source, encoding="utf-8")

    chosen = get_backend(backend_name.strip().lower()) if backend_name and backend_name.strip() else None
    if chosen is None:
        chosen = auto_select("systemverilog", is_sequential=mod.is_sequential, rtl_source=rtl_source)
    if chosen is None or not chosen.is_available():
        return None, None, "No simulator backend available.", tb_source

    result = chosen.run(rtl_path, tb_path, work, top=f"tb_{mod.name}")
    if not result.vcd_path:
        return None, None, result.log, tb_source

    write_dut_info(work, mod)
    module_info = load_module_info(work) or {}
    wave_json = vcd_to_json(result.vcd_path, module_info=module_info)
    vcd_signals = wave_json.get("signals") if isinstance(wave_json, dict) else None
    if not isinstance(vcd_signals, list) or not vcd_signals:
        return None, result.vcd_path, result.log, tb_source

    dut_ports = [p.get("name") for p in (module_info.get("ports") or []) if p.get("name")]
    toggle_cov = compute_toggle_coverage(vcd_signals, dut_ports)
    env_by_time = build_env_timeline_from_vcd_signals(vcd_signals)
    interp = CoverageInterpreter(rtl_source, filename="dut.v")
    interp.execute_over_timeline(env_by_time)
    stmt_cov = interp.get_statement_coverage()
    branch_cov = interp.get_branch_coverage()
    fsm_cov = (
        compute_fsm_coverage(mod, vcd_signals)
        if mod.is_sequential
        else FsmCoverage.not_applicable_result("Design is combinational — no state register detected")
    )
    overall = compute_overall_percent(stmt_cov, branch_cov, toggle_cov, fsm_cov, is_sequential=mod.is_sequential)
    cov = CoverageReport(
        statement=stmt_cov, branch=branch_cov, toggle=toggle_cov, fsm=fsm_cov,
        overall_percent=overall, meta={"dut": mod.name},
    )
    return cov, result.vcd_path, result.log, tb_source


def run_closure_loop(
    rtl_source: str,
    top_module: str | None = None,
    backend_name: str | None = None,
    max_iterations: int = 5,
    target_percent: float = 95.0,
    work_dir: Path | None = None,
) -> dict:
    """The full agentic loop: propose -> apply -> re-measure -> repeat.
    p3-3 (apply+regenerate), p3-4 (rerun+delta), p3-5 (stop condition).
    """
    mod = analyze_rtl(rtl_source, top_module=top_module)
    base = work_dir or Path(tempfile.mkdtemp(prefix="coverage_closure_"))
    base.mkdir(parents=True, exist_ok=True)

    accumulated: list[dict] = []
    iterations: list[dict] = []
    prev_percent = 0.0
    stagnant_rounds = 0
    stop_reason = "max iterations reached"
    last_cov: CoverageReport | None = None
    last_vcd: Path | None = None
    last_log = ""
    last_tb = ""
    last_work: Path = base

    for it in range(max(1, max_iterations)):
        gaps = extract_gaps(last_cov) if last_cov is not None else None
        try:
            new_vectors = propose_vectors(mod, rtl_source, gaps)
        except llm_client.LLMNotConfigured as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001 — surface any LLM failure, don't 500
            return {"error": f"Vector proposal failed: {e}"}

        if not new_vectors:
            stop_reason = "model proposed no new vectors"
            break
        accumulated.extend(new_vectors)

        work = base / f"iter_{it}"
        work.mkdir(parents=True, exist_ok=True)
        last_work = work
        cov, vcd_path, sim_log, tb_source = _run_and_measure(mod, rtl_source, accumulated, work, backend_name)
        last_log, last_tb = sim_log, tb_source
        if cov is None:
            stop_reason = "simulation or coverage computation failed"
            iterations.append({
                "iteration": it + 1, "num_new_vectors": len(new_vectors),
                "overall_percent": prev_percent, "delta": 0.0, "error": sim_log[-500:],
            })
            break

        last_cov, last_vcd = cov, vcd_path
        delta = cov.overall_percent - prev_percent
        iterations.append({
            "iteration": it + 1,
            "num_new_vectors": len(new_vectors),
            "num_total_vectors": len(accumulated),
            "overall_percent": cov.overall_percent,
            "delta": delta,
            "vector_rationales": [v.get("rationale", "") for v in new_vectors],
        })
        log_event({
            "module": mod.name, "iteration": it + 1, "num_new_vectors": len(new_vectors),
            "overall_percent": cov.overall_percent, "delta": delta,
        })

        if cov.overall_percent >= target_percent:
            stop_reason = "target reached"
            prev_percent = cov.overall_percent
            break
        if delta < 0.5:
            stagnant_rounds += 1
            if stagnant_rounds >= 2:
                stop_reason = "no improvement for 2 rounds"
                prev_percent = cov.overall_percent
                break
        else:
            stagnant_rounds = 0
        prev_percent = cov.overall_percent

    return {
        "module": mod.name,
        "iterations": iterations,
        "final_percent": prev_percent,
        "stop_reason": stop_reason,
        "num_total_vectors": len(accumulated),
        "coverage": last_cov.to_dict() if last_cov is not None else None,
        "testbench": last_tb,
        "sim_log": last_log[-4000:],
        "has_vcd": last_vcd is not None,
        "work_dir": last_work.as_posix(),
    }
