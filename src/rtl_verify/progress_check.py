"""Bounded forward-progress checking: deadlock, livelock and starvation as
solver questions about how long a request may wait.

Adapted from LUBIS EDA's "Deadlocks in SoCs: why livelock and starvation
escape" (lubis-eda.com). The article's argument is that these failures are
not "something went wrong now" bugs a simulation or a local protocol check
finds; they are the absence of progress, and they depend on rare combinations
of backpressure, arbitration order and occupancy. Its recommendations that
this module takes on:

  - state forward progress as an obligation (an accepted request must be
    answered; a request must be granted) and pair it with the usual safety
    checks, since "nothing bad happens" is satisfied by a design that does
    nothing;
  - assume only what the environment may do, and make FAIRNESS an explicit,
    reviewable assumption (a starvation counterexample can come from an
    environment that denies service forever, which is not a design bug);
  - require blockage to be bounded in time, not merely "eventually" resolved;
  - when a proof fails, read the counterexample structurally: which request
    stalled, and was the system frozen, spinning, or serving others.

How it is made checkable. A liveness claim ("eventually") is not a property
the open-source engines here prove directly, but its bounded form is an
ordinary safety property: a counter of cycles a request has waited, asserted
never to exceed N. That is exact for "served within N cycles", and PDR proves
it for all time, not just N steps. Fairness is encoded the same way, as a
counter of consecutive cycles an environment condition has been false,
assumed never to exceed M-1 (the condition holds at least once in every M
cycles), which is the standard safety encoding of "does not stay false
forever" at a stated bound.

What it does not claim.
  - "Eventually" without a bound is not proven. A FALSIFIED bound N means
    there is a counterexample at that bound, and a request that is never
    served is seen as FALSIFIED at every bound; `find_min_bound` reports "no
    bound up to the cap", which is evidence of starvation, not a proof of it.
  - The failure label is a heuristic over ONE counterexample trace (frozen
    state, activity without completion, or others being served) and is stated
    as such. It is not a deadlock proof.
  - A fairness assumption is a claim about the environment and has to be
    justified by the user; the check reports whether each one was needed for
    the proof, so unneeded ones can be dropped.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .analyzer import PortDirection, RtlModule
from .formal_props import generate_formal_wrapper, recommended_engine_chain
from .waveform import vcd_to_json

Property = Tuple[str, str, str]


@dataclass
class ProgressSpec:
    name: str
    request: str            # expression: a request is being made this cycle
    response: str           # expression: the request is answered this cycle
    bound: int              # must be answered within this many cycles
    competing: str = ""     # optional expression: someone ELSE is served this cycle
    sticky: bool = True     # a request stays pending until answered, even if `request` drops


@dataclass
class Fairness:
    name: str
    expr: str               # environment condition that must hold ...
    within: int             # ... at least once in every `within` cycles


@dataclass
class ProgressResult:
    spec: ProgressSpec
    verdict: str            # PROVEN | FALSIFIED | UNKNOWN | ERROR
    note: str = ""
    classification: Optional[str] = None       # STARVATION | DEADLOCK_LIKE | LIVELOCK_LIKE
    evidence: Dict[str, object] = field(default_factory=dict)
    needed_fairness: List[str] = field(default_factory=list)
    unneeded_fairness: List[str] = field(default_factory=list)
    trace_vcd: Optional[Path] = None

    def view(self) -> dict:
        return {
            "name": self.spec.name, "request": self.spec.request, "response": self.spec.response,
            "bound": self.spec.bound, "verdict": self.verdict, "note": self.note,
            "classification": self.classification, "evidence": self.evidence,
            "needed_fairness": self.needed_fairness, "unneeded_fairness": self.unneeded_fairness,
        }


def _width(n: int) -> int:
    return max(2, (n + 2).bit_length())


def _reset_active(module: RtlModule) -> str:
    if not module.reset_port:
        return "1'b0"
    return f"!{module.reset_port}" if module.reset_active_low else module.reset_port


def build_progress_wrapper(module: RtlModule, specs: Sequence[ProgressSpec], fairness: Sequence[Fairness],
                           assume_props: Sequence[Property] = (), asserted: Optional[Sequence[int]] = None,
                           active_fairness: Optional[Sequence[int]] = None) -> str:
    """The normal wrapper plus monitor registers. `asserted` selects which
    specs' bounds are asserted (default: all); `active_fairness` selects which
    fairness assumptions are applied (default: all)."""
    if not module.clock_port or not module.is_sequential:
        raise ValueError("progress checking needs a clocked design")
    clk = module.clock_port
    rst = _reset_active(module)
    asserted = list(range(len(specs))) if asserted is None else list(asserted)
    active_fairness = list(range(len(fairness))) if active_fairness is None else list(active_fairness)

    mon: List[str] = []
    props: List[Property] = list(assume_props)
    for k, s in enumerate(specs):
        w = _width(s.bound)
        cond = f"({s.request}) || __pend_{k}" if s.sticky else f"({s.request})"
        oth = (f"            if ({s.competing}) __oth_{k} <= (__oth_{k} == 16'hFFFF) ? __oth_{k} : __oth_{k} + 16'd1;\n"
               if s.competing else "")
        mon.append(
            f"    (* keep *) reg __pend_{k} = 1'b0;\n"
            f"    reg [{w - 1}:0] __wait_{k} = {w}'d0;\n"
            f"    (* keep *) reg [15:0] __oth_{k} = 16'd0;\n"
            f"    always @(posedge {clk}) begin\n"
            f"        if ({rst} || ({s.response})) begin __pend_{k} <= 1'b0; __wait_{k} <= {w}'d0; __oth_{k} <= 16'd0; end\n"
            f"        else if ({cond}) begin\n"
            f"            __pend_{k} <= 1'b1;\n"
            f"            if (__wait_{k} <= {w}'d{s.bound}) __wait_{k} <= __wait_{k} + {w}'d1;\n"
            f"{oth}"
            f"        end else begin __pend_{k} <= 1'b0; __wait_{k} <= {w}'d0; __oth_{k} <= 16'd0; end\n"
            f"    end\n")
        if k in asserted:
            # Disabled while reset is asserted, as `disable iff (reset)` would
            # be: a reset cancels the pending request, and a counterexample
            # whose only "activity" is the reset itself would be an artifact.
            props.append((f"progress_{k}", f"({rst}) || (__wait_{k} <= {w}'d{s.bound})", "assert"))
    for j, f in enumerate(fairness):
        w = _width(f.within)
        mon.append(
            f"    reg [{w - 1}:0] __fair_{j} = {w}'d0;\n"
            f"    always @(posedge {clk}) begin\n"
            f"        if ({rst} || ({f.expr})) __fair_{j} <= {w}'d0;\n"
            f"        else if (__fair_{j} <= {w}'d{f.within}) __fair_{j} <= __fair_{j} + {w}'d1;\n"
            f"    end\n")
        if j in active_fairness:
            props.append((f"fair_{j}", f"__fair_{j} <= {w}'d{max(f.within - 1, 0)}", "assume"))
    if not any(k == "assert" for _n, _e, k in props):
        raise ValueError("no progress property selected")
    text = generate_formal_wrapper(module, props)
    marker = "`ifdef FORMAL"
    idx = text.index(marker)
    return text[:idx] + "\n".join(mon) + "\n" + text[idx:]


class ProgressChecker:
    def __init__(self, module: RtlModule, rtl_path: Path, backend, work_root: Optional[Path] = None,
                 timeout_sec: int = 120, depth_override: int = 0):
        self.module, self.rtl_path, self.backend = module, rtl_path, backend
        self.work_root = work_root or Path(tempfile.mkdtemp(prefix="progress_"))
        self.timeout_sec, self.depth_override = timeout_sec, depth_override
        self.runs = 0

    def _run(self, wrapper_text: str):
        self.runs += 1
        d = self.work_root / f"run_{self.runs}"
        d.mkdir(parents=True, exist_ok=True)
        wrapper = d / "wrapper.sv"
        wrapper.write_text(wrapper_text, encoding="utf-8")
        chain = recommended_engine_chain(self.module, kind="assert", depth_override=self.depth_override)
        per = max(30, self.timeout_sec // len(chain))
        res = None
        for i, cfg in enumerate(chain):
            res = self.backend.run(self.rtl_path, wrapper, d / f"engine_{i}",
                                   top=f"{self.module.name}_formal_top", depth=cfg["depth"],
                                   mode=cfg["mode"], engine=cfg["engine"], timeout_sec=per)
            if res.status in ("PASS", "FAIL"):
                break
        return res

    # -- one bound ----------------------------------------------------------
    def check(self, spec: ProgressSpec, fairness: Sequence[Fairness] = (),
              assume_props: Sequence[Property] = (), classify: bool = True,
              minimize_fairness: bool = True) -> ProgressResult:
        try:
            res = self._run(build_progress_wrapper(self.module, [spec], fairness, assume_props))
        except ValueError as e:
            return ProgressResult(spec, "ERROR", str(e))
        if res.status == "FAIL":
            out = ProgressResult(spec, "FALSIFIED", f"served later than {spec.bound} cycles is reachable",
                                 trace_vcd=res.vcd_path)
            if classify:
                self._classify(out, k=0)
            return out
        if res.status == "PASS":
            out = ProgressResult(spec, "PROVEN",
                                 f"always served within {spec.bound} cycles"
                                 + (" (under the stated fairness assumptions)" if fairness else ""))
            if fairness and minimize_fairness:
                for j, f in enumerate(fairness):
                    keep = [i for i in range(len(fairness)) if i != j]
                    try:
                        r2 = self._run(build_progress_wrapper(self.module, [spec], fairness, assume_props,
                                                              active_fairness=keep))
                    except ValueError:
                        continue
                    (out.unneeded_fairness if r2.status == "PASS" else out.needed_fairness).append(f.name)
            return out
        if res.status == "ERROR":
            return ProgressResult(spec, "ERROR", "the monitor did not elaborate; check the expressions")
        return ProgressResult(spec, "UNKNOWN", f"solver ended {res.status}")

    # -- counterexample reading --------------------------------------------
    def _classify(self, out: ProgressResult, k: int) -> None:
        if out.trace_vcd is None:
            return
        wf = vcd_to_json(out.trace_vcd, module=self.module)
        if "error" in wf:
            return
        by_base: Dict[str, dict] = {}
        for sig in wf["signals"]:
            by_base.setdefault(sig["name"].split(".")[-1], sig)
        pend = by_base.get(f"__pend_{k}")
        if pend is None:
            return
        # The VCD carries one entry per step whether or not the value moved, so
        # work with values, not entries. The wait began at the last 0 -> 1 edge
        # of the pending flag; the violation is the first step at which the
        # wait counter exceeded the bound (the trace runs a step past it).
        def value_at(sig: dict, time: int) -> str:
            v = sig["transitions"][0]["value"]
            for t in sig["transitions"]:
                if t["time"] <= time:
                    v = t["value"]
            return v

        prev, t0 = "0", None
        for t in pend["transitions"]:
            if t["value"] == "1" and prev != "1":
                t0 = t["time"]
            prev = t["value"]
        if t0 is None:
            return
        wait = by_base.get(f"__wait_{k}")
        t_end = None
        if wait is not None:
            for t in wait["transitions"]:
                if set(t["value"]) <= {"0", "1"} and int(t["value"], 2) > out.spec.bound and t["time"] >= t0:
                    t_end = t["time"]
                    break
        times = sorted({t["time"] for s_ in wf["signals"] for t in s_["transitions"]})
        t_end = t_end if t_end is not None else times[-1]
        step = min((b2 - a2 for a2, b2 in zip(times, times[1:]) if b2 > a2), default=1)
        outputs = [p.name for p in self.module.ports if p.direction == PortDirection.OUTPUT]
        inputs = [p.name for p in self.module.ports
                  if p.direction == PortDirection.INPUT and p.name not in (self.module.clock_port,)]

        # The design legitimately moves right after accepting the request, so
        # "did anything change since the request" would call a deadlock a
        # livelock. Judge the TAIL of the wait: a frozen design is still in
        # its last few steps, a spinning one is not.
        tail_start = max(t0, t_end - 4 * step)

        def changed(names: List[str]) -> List[str]:
            hit = []
            for n in names:
                sig = by_base.get(n)
                if not sig:
                    continue
                ref = value_at(sig, tail_start)
                if any(t["time"] > tail_start and t["time"] <= t_end and t["value"] != ref
                       for t in sig["transitions"]):
                    hit.append(n)
            return hit

        out_changed, in_changed = changed(outputs), changed(inputs)
        oth = by_base.get(f"__oth_{k}")
        served = 0
        if oth:
            for t in oth["transitions"]:
                if t["time"] >= t0:
                    try:
                        served = max(served, int(t["value"], 2) if set(t["value"]) <= {"0", "1"} else 0)
                    except ValueError:
                        pass
        out.evidence = {"stall_started_at": t0, "violated_at": t_end, "judged_from": tail_start,
                        "outputs_changed_while_waiting": out_changed,
                        "inputs_changed_while_waiting": in_changed, "others_served_while_waiting": served}
        if served > 0:
            out.classification = "STARVATION"
            out.note += "; the resource was granted to others while this request waited"
        elif not out_changed:
            out.classification = "DEADLOCK_LIKE"
            out.note += ("; no output or exposed register changed in the last steps of the wait "
                         "(frozen); evidence lists which inputs did change")
        else:
            out.classification = "LIVELOCK_LIKE"
            out.note += "; the design kept changing state while the request stayed unanswered"
        out.note += " (heuristic label from one counterexample, not a proof)"

    # -- smallest bound --------------------------------------------------------
    def find_min_bound(self, spec: ProgressSpec, fairness: Sequence[Fairness] = (),
                       assume_props: Sequence[Property] = (), max_bound: int = 32) -> dict:
        """Smallest N for which the response bound is PROVEN, or None up to
        `max_bound`. Doubling then bisection; every probe is a real proof."""
        def proven(n: int) -> Optional[bool]:
            r = self.check(ProgressSpec(spec.name, spec.request, spec.response, n, spec.competing, spec.sticky),
                           fairness, assume_props, classify=False, minimize_fairness=False)
            return True if r.verdict == "PROVEN" else (False if r.verdict == "FALSIFIED" else None)

        probes: Dict[int, Optional[bool]] = {}
        lo, hi = 0, None
        n = 1
        while n <= max_bound:
            probes[n] = proven(n)
            if probes[n] is None:
                return {"min_bound": None, "status": "INCONCLUSIVE", "probes": probes}
            if probes[n]:
                hi = n
                break
            lo = n
            n *= 2
        if hi is None:
            if max_bound not in probes:
                probes[max_bound] = proven(max_bound)
                if probes[max_bound]:
                    hi = max_bound
            if hi is None:
                return {"min_bound": None, "status": "NO_BOUND_UP_TO_CAP", "probes": probes,
                        "note": f"every bound up to {max_bound} has a counterexample: consistent with "
                                "starvation, but unbounded starvation is not proven by this"}
        while hi - lo > 1:
            mid = (lo + hi) // 2
            p = proven(mid)
            probes[mid] = p
            if p is None:
                return {"min_bound": None, "status": "INCONCLUSIVE", "probes": probes}
            if p:
                hi = mid
            else:
                lo = mid
        return {"min_bound": hi, "status": "FOUND", "probes": probes}
