"""Execute a strategy plan, one technique at a time, stopping per property at
the first result that settles it.

`strategy_planner.py` decides which techniques a design needs. This module
runs that decision, with four rules that keep the answer meaningful:

  1. EXACT TRANSFORMS GO IN THE BASELINE. ROM-to-case and assertion
     decomposition change neither the design's behaviour nor what a verdict
     means, so they are part of the first run, not a separate attempt.
  2. ONE TECHNIQUE AT A TIME, ONLY ON WHAT IS STILL OPEN. A property that has a
     definitive result is never re-run under a technique. Each later attempt
     receives only the properties that are still inconclusive (plus every
     assumption), so a technique is never credited with, or blamed for, a
     result it did not produce.
  3. A RESULT IS LABELLED BY WHAT PRODUCED IT. A PROVEN under a technique that
     is sound for PROVEN is a proof for the real design. A FALSIFIED under one
     that may add behaviours is reported as UNCONFIRMED and is never presented
     as a bug. A PROVEN for a reduced configuration is reported as that, never
     as a proof of the real one. Exact techniques carry both verdicts.
  4. NOTHING THE GOAL FORBIDS IS RUN. Steps the plan marks
     NOT_ACCEPTABLE_FOR_GOAL are skipped with the reason, steps that live at
     another endpoint are listed for the user to run, and a technique that
     needs a protocol decision (case splits, fairness) is never invented.

The solver runs go through an injected `run_formal` callable (in the API it is
`formal_check` itself), so the control flow here is tested without a solver.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set

from .strategy_planner import Plan, Step

INCONCLUSIVE = {"TIMEOUT", "UNKNOWN", "CANCELLED"}
DEFINITIVE = {"PROVEN", "FALSIFIED", "REACHED", "UNREACHED"}
# Techniques run as an escalation, in the plan's own order of presentation.
_ESCALATION_ORDER = ["helper_invariants", "counter_abstraction", "black_boxing", "cut_points",
                     "data_width_reduction", "parameter_reduction"]
_BASELINE_TRANSFORMS = ["rom_to_case", "assertion_decomposition"]

RunFormal = Callable[..., Awaitable[dict]]


@dataclass
class Attempt:
    index: int
    technique: str
    apply_with: Dict[str, Any]
    properties_run: List[str]
    verdicts: Dict[str, str]
    soundness: str
    note: str = ""

    def view(self) -> dict:
        return {"index": self.index, "technique": self.technique, "apply_with": self.apply_with,
                "properties_run": self.properties_run, "verdicts": self.verdicts,
                "soundness": self.soundness, "note": self.note}


@dataclass
class Outcome:
    name: str
    final: str = "INCONCLUSIVE"       # see _label()
    raw_verdict: str = "UNKNOWN"
    resolved_by: Optional[str] = None
    meaning: str = ""
    trail: List[str] = field(default_factory=list)

    def view(self) -> dict:
        return {"name": self.name, "final": self.final, "raw_verdict": self.raw_verdict,
                "resolved_by": self.resolved_by, "meaning": self.meaning, "trail": self.trail}


@dataclass
class ExecutionReport:
    module: str
    goal: str
    attempts: List[Attempt]
    outcomes: Dict[str, Outcome]
    skipped: List[dict]
    stopped_because: str
    summary: str

    def view(self) -> dict:
        return {"module": self.module, "goal": self.goal, "summary": self.summary,
                "stopped_because": self.stopped_because,
                "outcomes": [o.view() for o in self.outcomes.values()],
                "attempts": [a.view() for a in self.attempts], "skipped": self.skipped}


def _kind(soundness: str) -> str:
    s = soundness.upper()
    if s.startswith("EXACT"):
        return "exact"
    if s.startswith("SOUND FOR PROVEN"):
        return "proven_only"
    if s.startswith("BOUNDED"):
        return "bounded"
    return "unknown"


def _label(verdict: str, technique: str, kind: str) -> tuple[str, str]:
    """(final label, what it means) for a definitive verdict from a technique."""
    if verdict in ("UNREACHED", "REACHED"):
        if kind == "exact":
            return verdict, "Reachability answered on the real design."
        if verdict == "REACHED" and kind == "proven_only":
            return "REACHED_UNDER_ABSTRACTION", ("Reached under an abstraction that adds behaviours: confirm "
                                                  "the trace on the real design.")
        if verdict == "UNREACHED" and kind == "proven_only":
            return "UNREACHED", "An abstraction that only adds behaviours still could not reach it."
        return f"{verdict}_UNCONFIRMED", f"Produced under a bounded or unclassified technique ({technique})."
    if kind == "exact":
        return verdict, "Holds as stated on the real design." if verdict == "PROVEN" else \
            "A real counterexample on the real design."
    if kind == "proven_only":
        if verdict == "PROVEN":
            return "PROVEN", (f"Proven with {technique}, which only adds behaviours: the property holds on the "
                              "real design.")
        return "FALSIFIED_UNCONFIRMED", (f"Counterexample found under {technique}, which can add behaviours: "
                                         "check the trace against the real design before calling it a bug.")
    if kind == "bounded":
        if verdict == "PROVEN":
            return "PROVEN_FOR_REDUCED_CONFIG", ("Holds only for the reduced configuration, not the real one: "
                                                 "not a proof of the design.")
        return "FALSIFIED_UNCONFIRMED", ("Counterexample at a reduced configuration: usually carries over, "
                                         "confirm at full size.")
    return f"{verdict}_UNCONFIRMED", f"Produced under {technique}, whose soundness class is not recorded."


async def execute_plan(
    plan: Plan,
    run_formal: RunFormal,
    base_args: Dict[str, Any],
    properties: List[dict],
    allowed_params: Set[str],
    max_attempts: int = 6,
    order: Optional[Sequence[str]] = None,
    proactive: bool = False,
    replay: Optional[Callable[..., Awaitable[Any]]] = None,
) -> ExecutionReport:
    """`base_args` are the formal-check arguments common to every attempt
    (rtl_text, top_module, timeout_sec ...); `properties` is the user's list
    (asserts, covers and assumes).

    `proactive`: when the plan RECOMMENDS an abstraction that is exact or
    sound for PROVEN (large design with a black-boxable block or a wide
    accumulator), apply it FIRST instead of waiting for the plain proof to
    fail, since a slow plain proof is the cost being avoided. A PROVEN is then
    a real proof. A FALSIFIED may be an artifact, so `replay(name, expr, trace)`
    runs the counterexample on the real design: CONFIRMED is a real bug, and
    anything else sends that property back to the plain proof. Nothing is
    reported on the strength of the abstraction alone."""
    props = [{**p, "kind": p.get("kind", "assert")} for p in properties]
    assumes = [p for p in props if p["kind"] == "assume"]
    targets = [p for p in props if p["kind"] != "assume"]
    names = [str(p["name"]) for p in targets]
    outcomes = {n: Outcome(n) for n in names}
    attempts: List[Attempt] = []
    skipped: List[dict] = []
    last_traces: Dict[str, Any] = {}
    by_tech = {s.technique: s for s in plan.steps}

    def _skip(step: Step, why: str) -> None:
        skipped.append({"technique": step.technique, "decision": step.decision, "why": why})

    if not targets:
        return ExecutionReport(plan.module, plan.goal, [], outcomes, skipped, "no properties to prove",
                               "No assert or cover properties were supplied, so there is nothing to execute. "
                               "Generate some first (see assertion_generation in the plan).")

    async def run(technique: str, apply_with: Dict[str, Any], open_names: Sequence[str], soundness: str) -> Attempt:
        subset = assumes + [p for p in targets if str(p["name"]) in open_names]
        args = {**base_args, **apply_with, "properties": json.dumps(subset)}
        idx = len(attempts) + 1
        try:
            out = await run_formal(**args)
        except Exception as e:  # noqa: BLE001 - a failed attempt is recorded, not fatal
            return Attempt(idx, technique, apply_with, list(open_names), {}, soundness,
                           f"the attempt failed to run: {type(e).__name__}: {e}")
        if "error" in out and not out.get("properties"):
            return Attempt(idx, technique, apply_with, list(open_names), {}, soundness, str(out["error"]))
        verdicts = {str(r.get("name")): str(r.get("verdict")) for r in out.get("properties", [])
                    if str(r.get("name")) in open_names}
        for r in out.get("properties", []):
            if r.get("waveform_json"):
                last_traces[str(r.get("name"))] = r["waveform_json"]
        return Attempt(idx, technique, apply_with, list(open_names), verdicts, soundness)

    def absorb(att: Attempt, kind: str) -> List[str]:
        """Record verdicts; return the names that are still open."""
        still = []
        for n in att.properties_run:
            o = outcomes[n]
            v = att.verdicts.get(n)
            if v is None:
                o.trail.append(f"{att.technique}: no result ({att.note or 'attempt produced none'})")
                still.append(n)
                continue
            o.trail.append(f"{att.technique}: {v}")
            if v in DEFINITIVE:
                o.raw_verdict = v
                o.final, o.meaning = _label(v, att.technique, kind)
                o.resolved_by = att.technique
            elif v == "ERROR":
                o.raw_verdict, o.final = "ERROR", "ERROR"
                o.meaning = "The tool could not elaborate this property; fix its expression."
                o.resolved_by = att.technique
            else:
                o.raw_verdict = v
                still.append(n)
        return still

    # 1. baseline: plain proof plus exact transforms ------------------------------
    base_apply: Dict[str, Any] = {}
    applied_exact: List[str] = []
    for t in _BASELINE_TRANSFORMS:
        step = by_tech.get(t)
        if step and step.decision == "RECOMMENDED":
            ok = {k: v for k, v in step.apply_with.items() if k in allowed_params}
            if ok:
                base_apply.update(ok)
                applied_exact.append(t)
    label = "plain_proof" + ("".join(f" + {t}" for t in applied_exact))

    proactive_step: Optional[Step] = None
    if proactive:
        for tech in ("black_boxing", "cut_points", "counter_abstraction", "data_width_reduction"):
            st = by_tech.get(tech)
            if (st is not None and st.decision == "RECOMMENDED" and st.apply_with
                    and all(k in allowed_params for k in st.apply_with)
                    and _kind(st.soundness) in ("exact", "proven_only")):
                proactive_step = st
                break

    if proactive_step is not None:
        kind = _kind(proactive_step.soundness)
        att = await run(f"{label} + {proactive_step.technique}", {**base_apply, **proactive_step.apply_with},
                        names, proactive_step.soundness)
        attempts.append(att)
        open_names = absorb(att, kind)
        for n in list(att.properties_run):
            o = outcomes[n]
            if not (o.final == "FALSIFIED_UNCONFIRMED" and o.resolved_by == att.technique):
                continue
            expr = next(str(p.get("expr", "")) for p in targets if str(p["name"]) == n)
            res = await replay(n, expr, last_traces.get(n)) if replay is not None else None
            status = getattr(res, "status", "UNAVAILABLE")
            if status == "CONFIRMED":
                o.final = "FALSIFIED"
                o.meaning = (f"Counterexample found under {proactive_step.technique} and reproduced on the real "
                             "design by replay: a real violation.")
                o.trail.append(f"replay on the real design: CONFIRMED at step {getattr(res, 'violated_at_step', '?')}")
            else:
                o.trail.append(f"replay on the real design: {status}; falling back to the plain proof")
                o.final, o.meaning, o.resolved_by, o.raw_verdict = "INCONCLUSIVE", "", None, "UNKNOWN"
                open_names.append(n)
        if open_names:
            att2 = await run(label + " (real design)", base_apply, list(open_names), "EXACT")
            attempts.append(att2)
            open_names = absorb(att2, "exact")
    else:
        att = await run(label, base_apply, names, "EXACT")
        attempts.append(att)
        open_names = absorb(att, "exact")
    stopped = "every property was settled by the baseline" if not open_names else ""

    # 2. escalate, one technique at a time, only on what is still open --------------
    for tech in (list(order) if order is not None else _ESCALATION_ORDER):
        if not open_names:
            break
        step = by_tech.get(tech)
        if step is None or (proactive_step is not None and tech == proactive_step.technique):
            continue
        if step.decision == "NOT_ACCEPTABLE_FOR_GOAL":
            _skip(step, "its answer is not valid for this goal: " + step.reason)
            continue
        if step.decision not in ("IF_STUCK", "RECOMMENDED"):
            continue
        unknown = [k for k in step.apply_with if k not in allowed_params]
        if unknown or not step.apply_with:
            _skip(step, f"not runnable through the formal check ({unknown or 'no parameters'}); "
                        f"use: {step.apply_with.get('endpoint', 'see plan')}")
            continue
        if len(attempts) >= max_attempts:
            stopped = f"the attempt limit ({max_attempts}) was reached with properties still open"
            break
        att = await run(tech, dict(step.apply_with), open_names, step.soundness)
        attempts.append(att)
        open_names = absorb(att, _kind(step.soundness))
    if not stopped:
        stopped = ("every property was settled" if not open_names
                   else "the plan's techniques are exhausted with properties still open")

    for n in open_names:
        outcomes[n].final = "INCONCLUSIVE"
        outcomes[n].meaning = ("No technique in the plan produced a result. The trail shows what was tried; "
                               "more time or a manual technique (case split, your own cut points) is next.")

    # 3. steps this module never runs, listed so the user knows --------------------
    for step in plan.steps:
        if step.technique in ("plain_proof",) or step.technique in _BASELINE_TRANSFORMS or \
                step.technique in _ESCALATION_ORDER:
            continue
        if step.decision in ("RECOMMENDED", "IF_STUCK", "AFTER_PROOF", "OPTIONAL"):
            _skip(step, "run separately: " + str(step.apply_with.get("endpoint", step.apply_with or "see plan")))

    settled = [o for o in outcomes.values() if o.final not in ("INCONCLUSIVE",)]
    summary = (f"{len(settled)} of {len(outcomes)} properties settled in {len(attempts)} attempt(s); "
               + "; ".join(f"{o.name}: {o.final}" + (f" (by {o.resolved_by})" if o.resolved_by else "")
                           for o in outcomes.values()) + ".")
    return ExecutionReport(plan.module, plan.goal, attempts, outcomes, skipped, stopped, summary)
