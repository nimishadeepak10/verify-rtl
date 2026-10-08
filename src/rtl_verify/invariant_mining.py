"""Helper-invariant mining: propose candidate invariants from simulation,
keep only the ones a solver proves, and hand them to the hard proof.

Why this exists. A proof by induction fails when the property is true but not
inductive: the solver finds a state, unreachable in practice, that satisfies
the property now and violates it one step later. The standard remedy in the
formal literature (Seligman et al., "Formal Verification", Ch. 10 and the
helper-assertion discussions in Siemens' Verification Horizons complexity
series) is a helper invariant: a second fact that is true of every reachable
state and rules that unreachable state out. Finding them by hand is the slow
part. This module mines them.

The pipeline is Daikon-style proposal followed by Houdini-style disposal:

  1. PROPOSE (heuristic). Run the design under constrained-random stimulus in
     Icarus Verilog and record every output and exposed internal register at
     each cycle after reset. From the recorded values, propose facts that held
     on every sample: a register only ever took a small set of values, a
     vector stayed one-hot, a counter never exceeded some bound, two flags were
     never high together, one flag always implied another, two same-width
     values were always equal or ordered. Simulation can only make a claim
     LOOK true, so nothing here is trusted.
  2. DISPOSE (exact). All candidates are asserted together in ONE k-induction
     run. Each round, every candidate the solver can violate is dropped:
     either the base case reached a violating state from reset (the claim is
     simply false) or the induction step did (true so far but not inductive
     relative to the rest). Repeat on the survivors until a run passes.
     The survivors are then jointly proven: each holds from reset and is
     preserved by one step given all of them. This is the Houdini algorithm;
     its fixed point is the largest inductive subset, so the answer does not
     depend on candidate order.
  3. USE. The surviving invariants are added as assumptions to the original
     property's proof. They are true of every reachable state, so assuming
     them removes only unreachable states: PROVEN stays PROVEN and FALSIFIED
     stays FALSIFIED. Unlike a cut point or a black box there is no direction
     in which the answer can be wrong, which is why this escalation is tried
     before any abstraction.

What is guaranteed and what is not.
  - Guaranteed: an invariant reported as proven holds in every reachable state
    of the design under the supplied assumptions (base case from reset, plus
    induction at the checked depth). Candidates that merely looked true in
    simulation are never used.
  - Not guaranteed: that the mining finds the invariant a given proof needs.
    Random simulation can miss a state, so a true bound may be proposed too
    tight and then dropped; and a needed invariant may be outside the shapes
    proposed. "No invariants found" never means "none exist".
  - Every candidate is guarded by the reset condition (`reset_active || inv`),
    since registers legitimately hold arbitrary values until the first reset
    edge; the guard weakens the fact only while reset is asserted.

Needs Icarus Verilog for the proposal step; with no simulator, only
user-supplied candidates (which still go through the same exact disposal)
are used.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .analyzer import PortDirection, RtlModule
from .formal_props import generate_formal_wrapper

Property = Tuple[str, str, str]

MAX_CANDIDATES = 60
MAX_ROUNDS = 12
_PHASE_CYCLES = 500
_VALUE_SET_LIMIT = 12
_PAIR_SIGNAL_LIMIT = 12


@dataclass
class Candidate:
    expr: str                # unguarded boolean expression over port names
    kind: str                # value_set | onehot | upper_bound | lower_bound | mutex | cover | implication | equal | ordered | constant | user
    signals: Tuple[str, ...] = ()
    origin: str = "simulation"  # "simulation" | "user"


@dataclass
class MiningReport:
    applicable: bool = True
    proposed: int = 0
    proven: List[Candidate] = field(default_factory=list)
    dropped: List[Tuple[Candidate, str]] = field(default_factory=list)  # (candidate, reason)
    rounds: int = 0
    samples: int = 0
    status: str = "NOT_RUN"     # PROVEN_SET | NONE_FOUND | INCONCLUSIVE | NOT_APPLICABLE
    note: str = ""
    guarded: List[Tuple[str, str]] = field(default_factory=list)  # (name, guarded expr) for the proven ones

    def view(self) -> dict:
        return {
            "status": self.status,
            "proposed": self.proposed,
            "samples": self.samples,
            "rounds": self.rounds,
            "proven": [{"expr": c.expr, "kind": c.kind, "origin": c.origin} for c in self.proven],
            "dropped": [{"expr": c.expr, "kind": c.kind, "reason": r} for c, r in self.dropped],
            "note": self.note,
        }

    def assumptions(self) -> List[Property]:
        """The proven invariants as assume-kind properties for the wrapper."""
        return [(name, expr, "assume") for name, expr in self.guarded]


# ----------------------------------------------------------------- simulation


def _observed_ports(mod: RtlModule) -> List:
    """Output ports worth recording: everything the design drives, minus the
    array-debug outputs (they read a free select index, not a single value)."""
    array_dbg = {p.name[len("__dbgsel_"):] for p in mod.inputs if p.name.startswith("__dbgsel_")}
    out = []
    for p in mod.outputs:
        if p.name.startswith("__dbg_") and p.name[len("__dbg_"):] in array_dbg:
            continue
        if 1 <= p.width <= 32:
            out.append(p)
    return out


def _build_testbench(mod: RtlModule, observed: Sequence) -> str:
    clk, rst = mod.clock_port, mod.reset_port
    active = "1'b0" if mod.reset_active_low else "1'b1"
    inactive = "1'b1" if mod.reset_active_low else "1'b0"
    decls, conns, drives, inits = [], [], [], []
    for p in mod.ports:
        rng = p.range_str()
        rng_sp = f"{rng} " if rng else ""
        conns.append(f".{p.name}({p.name})")
        if p.direction == PortDirection.INPUT:
            decls.append(f"    reg {rng_sp}{p.name};")
            inits.append(f"        {p.name} = 0;")
            if p.name not in (clk, rst):
                drives.append(
                    f"            case (mode) 1: {p.name} = $random(seed) & $random(seed) & $random(seed);"
                    f" 2: {p.name} = $random(seed) | $random(seed) | $random(seed);"
                    f" default: {p.name} = $random(seed); endcase")
        else:
            decls.append(f"    wire {rng_sp}{p.name};")
    fmt = " ".join("%0h" for _ in observed)
    args = ", ".join(p.name for p in observed)
    rst_line = f"            {rst} = ((cyc < 3) || (cyc >= {2 * _PHASE_CYCLES + 40} && cyc < {2 * _PHASE_CYCLES + 42})) ? {active} : {inactive};" if rst else ""
    rst_ok = f"({rst} === {inactive})" if rst else "1"
    total = 4 * _PHASE_CYCLES
    return f"""`timescale 1ns/1ps
module __mine_tb;
{chr(10).join(decls)}
    integer cyc, seed, mode;
    {mod.name} dut ({", ".join(conns)});
    initial {clk} = 0;
    always #5 {clk} = ~{clk};
    initial begin
        seed = 12345;
{chr(10).join(inits)}
        {rst + " = " + active + ";" if rst else ""}
        for (cyc = 0; cyc < {total}; cyc = cyc + 1) begin
            @(posedge {clk}); #1;
            mode = (cyc / {_PHASE_CYCLES}) % 3;
{chr(10).join(drives)}
{rst_line}
        end
        $finish;
    end
    always @(negedge {clk}) if (cyc > 4 && {rst_ok}) $display("S {fmt}", {args});
endmodule
"""


def collect_traces(mod: RtlModule, rtl_source: str, work_dir: Path,
                   timeout_sec: int = 60) -> Tuple[Optional[Dict[str, List[int]]], Dict[str, int], str]:
    """Run constrained-random simulation. Returns (columns by signal name or
    None, widths, note)."""
    from .backends.icarus import find_iverilog, find_vvp

    if not mod.clock_port or not mod.is_sequential:
        return None, {}, "combinational design: no state to mine invariants over"
    iverilog = find_iverilog()
    vvp = find_vvp(iverilog) if iverilog else None
    if not (iverilog and vvp):
        return None, {}, "Icarus Verilog not found: no simulation-derived candidates"
    observed = _observed_ports(mod)
    if not observed:
        return None, {}, "no observable signals"
    work_dir.mkdir(parents=True, exist_ok=True)
    dut = work_dir / "dut.sv"
    dut.write_text(rtl_source, encoding="utf-8")
    tb = work_dir / "mine_tb.sv"
    tb.write_text(_build_testbench(mod, observed), encoding="utf-8")
    exe = work_dir / "mine.vvp"
    comp = subprocess.run([iverilog, "-g2012", "-o", str(exe), str(dut), str(tb)],
                          capture_output=True, text=True, timeout=timeout_sec)
    if comp.returncode != 0:
        return None, {}, "simulation compile failed: " + (comp.stderr or comp.stdout).strip().splitlines()[0][:160]
    try:
        run = subprocess.run([vvp, str(exe)], capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return None, {}, "simulation timed out"
    names = [p.name for p in observed]
    cols: Dict[str, List[int]] = {n: [] for n in names}
    for line in run.stdout.splitlines():
        if not line.startswith("S "):
            continue
        toks = line[2:].split()
        if len(toks) != len(names) or any(re.search(r"[xXzZ]", t) for t in toks):
            continue
        for n, t in zip(names, toks):
            cols[n].append(int(t, 16))
    widths = {p.name: p.width for p in observed}
    if not cols[names[0]]:
        return None, widths, "simulation produced no usable samples (reset never released, or only X values)"
    return cols, widths, ""


# ------------------------------------------------------------------ proposals


def propose_candidates(cols: Dict[str, List[int]], widths: Dict[str, int],
                       limit: int = MAX_CANDIDATES) -> List[Candidate]:
    """Facts that held on every sample. Heuristic by construction."""
    cands: List[Candidate] = []
    seen: set[str] = set()

    def add(expr: str, kind: str, sigs: Tuple[str, ...]) -> None:
        if expr not in seen:
            seen.add(expr)
            cands.append(Candidate(expr=expr, kind=kind, signals=sigs))

    # Signals with identical recordings (an output and the register it mirrors)
    # would multiply every candidate; keep one representative and state the
    # aliasing once as an equality.
    reps: Dict[Tuple[int, Tuple[int, ...]], str] = {}
    aliased: List[Tuple[str, str]] = []
    varying = []
    for n, v in cols.items():
        if len(set(v)) <= 1:
            continue
        key = (widths[n], tuple(v))
        if key in reps:
            aliased.append((reps[key], n))
        else:
            reps[key] = n
            varying.append(n)
    for keep, alias in aliased:
        add(f"{keep} == {alias}", "equal", (keep, alias))
    constants = [n for n, v in cols.items() if len(set(v)) == 1 and n.startswith("__dbg_")]

    for n in constants:
        add(f"{n} == {widths[n]}'d{cols[n][0]}", "constant", (n,))

    for n in varying:
        w, vals = widths[n], cols[n]
        distinct = sorted(set(vals))
        if w >= 2:
            if len(distinct) <= _VALUE_SET_LIMIT and len(distinct) < (1 << w):
                add(" || ".join(f"({n} == {w}'d{v})" for v in distinct), "value_set", (n,))
            if all(bin(v).count("1") == 1 for v in vals):
                add(f"$onehot({n})", "onehot", (n,))
            elif all(bin(v).count("1") <= 1 for v in vals):
                add(f"$onehot0({n})", "onehot", (n,))
            if distinct[-1] < (1 << w) - 1:
                add(f"{n} <= {w}'d{distinct[-1]}", "upper_bound", (n,))
            if distinct[0] > 0:
                add(f"{n} >= {w}'d{distinct[0]}", "lower_bound", (n,))

    bits = [n for n in varying if widths[n] == 1][:_PAIR_SIGNAL_LIMIT]
    for i, a in enumerate(bits):
        for b in bits[i + 1:]:
            pairs = set(zip(cols[a], cols[b]))
            if pairs <= {(0, 0), (1, 1)}:
                add(f"{a} == {b}", "equal", (a, b))
                continue
            if pairs <= {(0, 1), (1, 0)}:
                add(f"{a} != {b}", "equal", (a, b))
                continue
            if (1, 1) not in pairs:
                add(f"!({a} && {b})", "mutex", (a, b))
            if (0, 0) not in pairs:
                add(f"{a} || {b}", "cover", (a, b))
            if (1, 0) not in pairs:
                add(f"!{a} || {b}", "implication", (a, b))
            if (0, 1) not in pairs:
                add(f"!{b} || {a}", "implication", (b, a))

    by_width: Dict[int, List[str]] = {}
    for n in varying:
        if widths[n] >= 2:
            by_width.setdefault(widths[n], []).append(n)
    for group in by_width.values():
        group = group[:_PAIR_SIGNAL_LIMIT]
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                za = list(zip(cols[a], cols[b]))
                if all(x == y for x, y in za):
                    add(f"{a} == {b}", "equal", (a, b))
                elif all(x <= y for x, y in za):
                    add(f"{a} <= {b}", "ordered", (a, b))
                elif all(x >= y for x, y in za):
                    add(f"{b} <= {a}", "ordered", (b, a))

    priority = {"value_set": 0, "onehot": 1, "upper_bound": 2, "equal": 3, "mutex": 4, "implication": 5,
                "ordered": 6, "cover": 7, "lower_bound": 8, "constant": 9, "user": -1}
    cands.sort(key=lambda c: priority.get(c.kind, 10))
    return cands[:limit]


# -------------------------------------------------------------------- disposal


def _guard(mod: RtlModule, expr: str) -> str:
    """Invariants are claims about operation after reset."""
    if not mod.reset_port:
        return f"({expr})"
    active = f"!{mod.reset_port}" if mod.reset_active_low else mod.reset_port
    return f"({active}) || ({expr})"


_FAILED_SUMMARY = re.compile(r"failed assertion \S+\.(inv_\d+)\b")
_FAILED_LIVE = re.compile(r"Assert failed in \S+: (inv_\d+)\b")


def _failed_names(log: str) -> Tuple[set, set]:
    """(names violated in the base case, names violated in the induction step)."""
    base: set = set()
    induct: set = set()
    section = None
    for line in log.splitlines():
        if "counterexample trace [basecase]" in line:
            section = base
        elif "counterexample trace [induction]" in line:
            section = induct
        elif "summary:" in line and "failed assertion" not in line:
            section = None
        m = _FAILED_SUMMARY.search(line)
        if m and section is not None:
            section.add(m.group(1))
    return base, induct


def _houdini(mod: RtlModule, rtl_path: Path, cands: List[Candidate], assume_props: Sequence[Property],
             engine, work_dir: Path, timeout_sec: int, report: MiningReport,
             engine_kwargs: Optional[dict] = None) -> None:
    live = list(cands)
    for rnd in range(1, MAX_ROUNDS + 1):
        report.rounds = rnd
        if not live:
            report.status = "NONE_FOUND"
            return
        names = {f"inv_{k}": c for k, c in enumerate(live)}
        props = list(assume_props) + [(nm, _guard(mod, c.expr), "assert") for nm, c in names.items()]
        rdir = work_dir / f"round_{rnd}"
        rdir.mkdir(parents=True, exist_ok=True)
        wrapper = rdir / "wrapper.sv"
        wrapper.write_text(generate_formal_wrapper(mod, props), encoding="utf-8")
        res = engine.run(rtl_path, wrapper, rdir / "run", top=f"{mod.name}_formal_top",
                         depth=0, mode="prove", engine="smtbmc", timeout_sec=timeout_sec,
                         **(engine_kwargs or {}))
        if res.status == "PASS":
            report.proven = live
            report.guarded = [(f"__inv_{k}", _guard(mod, c.expr)) for k, c in enumerate(live)]
            report.status = "PROVEN_SET"
            return
        if res.status not in ("FAIL", "UNKNOWN"):
            report.status = "INCONCLUSIVE"
            report.note = f"disposal round {rnd} ended {res.status}; no candidate was trusted"
            return
        base_bad, ind_bad = _failed_names(res.log)
        drop = {nm: "false in a reachable state (base case)" for nm in base_bad}
        for nm in ind_bad:
            drop.setdefault(nm, "not inductive with the other candidates")
        drop = {nm: why for nm, why in drop.items() if nm in names}
        if not drop:
            report.status = "INCONCLUSIVE"
            report.note = f"disposal round {rnd} ended {res.status} but no violated candidate could be identified"
            return
        for nm, why in drop.items():
            report.dropped.append((names[nm], why))
        live = [c for nm, c in names.items() if nm not in drop]
    report.status = "INCONCLUSIVE"
    report.note = f"no fixed point after {MAX_ROUNDS} rounds"


def mine_invariants(mod: RtlModule, rtl_source: str, rtl_path: Path, assume_props: Sequence[Property],
                    engine, work_dir: Path, timeout_sec: int = 60,
                    user_candidates: Sequence[str] = (), max_candidates: int = MAX_CANDIDATES,
                    engine_kwargs: Optional[dict] = None) -> MiningReport:
    """Propose from simulation (plus any user-supplied expressions), dispose
    with Houdini. `mod`/`rtl_path` must be the probed module and its source
    file, so internal registers are visible as `__dbg_*` ports."""
    report = MiningReport()
    if not mod.is_sequential or not mod.clock_port:
        report.applicable = False
        report.status = "NOT_APPLICABLE"
        report.note = "combinational design: a single-step proof is already exhaustive"
        return report
    cols, widths, note = collect_traces(mod, rtl_source, work_dir / "sim")
    cands: List[Candidate] = []
    if cols is not None:
        report.samples = len(next(iter(cols.values())))
        cands = propose_candidates(cols, widths, max_candidates)
    else:
        report.note = note
    for ex in user_candidates:
        ex = ex.strip()
        if ex and all(ex != c.expr for c in cands):
            cands.insert(0, Candidate(expr=ex, kind="user", origin="user"))
    report.proposed = len(cands)
    if not cands:
        report.status = "NONE_FOUND"
        return report
    _houdini(mod, rtl_path, cands, assume_props, engine, work_dir / "houdini", timeout_sec, report, engine_kwargs)
    if report.status == "PROVEN_SET" and not report.proven:
        report.status = "NONE_FOUND"
    return report
