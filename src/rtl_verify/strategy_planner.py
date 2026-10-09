"""Proof strategy planner: which techniques does THIS design need for THIS goal?

The toolkit has many complexity-reduction and checking techniques, and a
design needs few of them. Black-boxing means nothing for a single module;
counter abstraction means nothing without a counter compared against a large
value; a ROM conversion means nothing without a ROM. Running every technique
on every design wastes solver time, and some of them change what a result
means, so applying them blindly is not neutral. This module looks at the
design (and, when given, the properties, assumptions and goal) and returns a
plan: which techniques are not applicable and why, which are applicable but
not needed, which to apply now, and which to keep in reserve.

It is RECOMMEND-ONLY. It runs no solver; every decision comes from structure
already visible in the source, through the same detectors the techniques
themselves use, so a recommendation and the technique's own applicability test
cannot disagree. Every step carries a reason built from evidence, and a skip
carries the reason for the skip so it can be questioned.

Decisions:
  RUN_FIRST               the baseline proof; always the first step
  RECOMMENDED             apply now: exact, cheap, or clearly warranted
  IF_STUCK                applicable; hold in reserve for an inconclusive result
  AFTER_PROOF             a check to run once a proof exists
  OPTIONAL                applicable, only worth it if you care about its question
  NOT_NEEDED              applicable, but this design/goal does not call for it
  NOT_ACCEPTABLE_FOR_GOAL applicable, but its answer is not valid for the goal
  NOT_APPLICABLE          the structure it works on is absent

Soundness is stated per technique, because the goal decides what is allowed:
a sign-off proof must not rest on a technique whose PROVEN covers only a
reduced configuration, while a bug hunt can use it freely.

Limits. Detection is textual (this project has no elaborated netlist), so a
structure hidden behind a macro or generate construct can be missed; a "not
applicable" is a statement about the source as written, with its reason, and
can be overridden by naming the signals yourself. What a fairness assumption
or a request-withdrawal rule should be is a protocol fact the tool cannot
know, and it says so rather than choosing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .analyzer import PortDirection, RtlModule, analyze_rtl
from .assertion_decompose import decompose_assertion
from .blackbox import _find_module_span, recommend_blackbox_candidates
from .counter_abstract import find_counter_candidates
from .cutpoint import recommend_cutpoint_candidates
from .data_independence import (
    analyze_data_independence, guess_data_signals, property_data_use, recommend_data_width_reductions,
)
from .param_reduce import recommend_parameter_reductions
from .rom_abstract import find_rom_candidates

GOALS = ("prove", "signoff", "find_bugs", "progress", "explore")

_SMALL_STATE_BITS = 128
_LARGE_STATE_BITS = 1024
_REQ = re.compile(r"(req|request|start|go\b|valid|cmd|enq|push)", re.IGNORECASE)
_RESP = re.compile(r"(ack|grant|gnt|done|ready|resp|complete|deq|pop)", re.IGNORECASE)
_OPCODE = re.compile(r"(^op$|opcode|^cmd|mode|^sel|func|instr)", re.IGNORECASE)


@dataclass
class Step:
    technique: str
    decision: str
    reason: str
    when: str = ""
    soundness: str = ""
    apply_with: Dict[str, object] = field(default_factory=dict)
    evidence: List[str] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)

    def view(self) -> dict:
        return {"technique": self.technique, "decision": self.decision, "when": self.when,
                "reason": self.reason, "soundness": self.soundness, "apply_with": self.apply_with,
                "evidence": self.evidence, "caveats": self.caveats}


@dataclass
class Plan:
    module: str
    goal: str
    facts: Dict[str, object]
    steps: List[Step]
    summary: str
    notes: List[str] = field(default_factory=list)

    def view(self) -> dict:
        return {"module": self.module, "goal": self.goal, "facts": self.facts,
                "summary": self.summary, "steps": [s.view() for s in self.steps], "notes": self.notes}

    def by_technique(self, name: str) -> Step:
        return next(s for s in self.steps if s.technique == name)


# --------------------------------------------------------------------- facts


def _state_bits(mod: RtlModule) -> int:
    total = 0
    for s in mod.internal_signals:
        entries = abs(s.array_hi - s.array_lo) + 1 if s.is_array else 1
        total += s.width * entries
    return total


def _defined_modules(rtl_source: str) -> List[str]:
    return re.findall(r"\bmodule\s+([A-Za-z_]\w*)", rtl_source)


def _instantiated(rtl_source: str, mod: RtlModule) -> List[str]:
    try:
        start, end = _find_module_span(rtl_source, mod.name)
    except ValueError:
        return []
    body = rtl_source[start:end]
    found = []
    for name in _defined_modules(rtl_source):
        if name != mod.name and re.search(rf"\b{re.escape(name)}\b\s*(?:#\s*\([^;]*?\)\s*)?[A-Za-z_]\w*\s*\(", body):
            found.append(name)
    return found


def gather_facts(rtl_source: str, top_module: str, rtl_mod: RtlModule) -> Dict[str, object]:
    subs = _instantiated(rtl_source, rtl_mod)
    bits = _state_bits(rtl_mod)
    for sub in subs:
        try:
            bits += _state_bits(analyze_rtl(rtl_source, top_module=sub))
        except Exception:  # noqa: BLE001 - a submodule that will not parse just is not counted
            pass
    clocked = rtl_mod.is_sequential and bool(rtl_mod.clock_port)
    if clocked and bits == 0:
        # A clocked design with no registers found means the source declares its
        # state in a way this textual scan cannot size (generate loops, packed
        # structs), not that it has none. Say so rather than call it small.
        size = "UNKNOWN"
    else:
        size = "SMALL" if bits <= _SMALL_STATE_BITS else ("MEDIUM" if bits <= _LARGE_STATE_BITS else "LARGE")
    ins = [p.name for p in rtl_mod.inputs if p.name not in (rtl_mod.clock_port, rtl_mod.reset_port)]
    outs = [p.name for p in rtl_mod.outputs]
    reqs = [n for n in ins if _REQ.search(n)]
    resps = [n for n in outs if _RESP.search(n)]
    return {
        "sequential": rtl_mod.is_sequential, "clocked": clocked, "multiple_clocks": rtl_mod.has_multiple_clocks,
        "has_fsm": bool(rtl_mod.state_reg), "fsm_states": len(rtl_mod.states),
        "state_bits_estimate": bits, "size_class": size,
        "internal_registers": len(rtl_mod.internal_signals),
        "memories": [s.name for s in rtl_mod.internal_signals if s.is_array],
        "submodules": subs, "hierarchical": bool(subs),
        "request_like_inputs": reqs, "response_like_outputs": resps,
        "opcode_like_inputs": [p.name for p in rtl_mod.inputs
                               if _OPCODE.search(p.name) and p.width >= 3],
    }


# --------------------------------------------------------------------- plan


def plan_strategy(rtl_source: str, top_module: str = "", goal: str = "prove",
                  properties: Optional[List[dict]] = None, data_signals: Optional[List[str]] = None,
                  has_spec: bool = False) -> Plan:
    if goal not in GOALS:
        raise ValueError(f"goal must be one of {GOALS}")
    mod = analyze_rtl(rtl_source, top_module=top_module.strip() or None)
    facts = gather_facts(rtl_source, top_module, mod)
    props = properties or []
    asserts = [p for p in props if str(p.get("kind", "assert")) == "assert"]
    assumes = [p for p in props if p.get("kind") == "assume"]
    facts["properties"] = {"asserts": len(asserts), "assumptions": len(assumes)}
    size = facts["size_class"]
    rigorous = goal in ("prove", "signoff", "progress")
    steps: List[Step] = []

    # 0. baseline ----------------------------------------------------------
    base_reason = ("Run the plain proof first through the engine chain (PDR, then k-induction with "
                   "different solvers). ")
    if size == "SMALL":
        base_reason += (f"The design is small (about {facts['state_bits_estimate']} state bits), so this "
                        "is expected to be enough on its own.")
    elif size == "UNKNOWN":
        base_reason += ("The design's state could not be sized from the source (it is clocked but no "
                        "registers were found, e.g. state declared in generate loops), so its size is "
                        "unknown: the plain result is the way to find out.")
    else:
        base_reason += (f"The design is {size.lower()} (about {facts['state_bits_estimate']} state bits): "
                        "expect to need more, but the plain result tells you where it stalls.")
    steps.append(Step("plain_proof", "RUN_FIRST", base_reason, when="first", soundness="EXACT",
                      apply_with={}))

    # 1. exact transforms ------------------------------------------------------
    roms = find_rom_candidates(rtl_source)
    elig = [r for r in roms if r.eligible]
    if elig:
        steps.append(Step(
            "rom_to_case", "RECOMMENDED",
            "A constant lookup table is replaced by an equivalent case statement: the memory's state "
            "bits disappear and nothing else changes.",
            when="before the first proof", soundness="EXACT", apply_with={"rom_to_case": True},
            evidence=[f"{r.module}.{r.name}: {r.depth} x {r.width} bits ({r.state_bits_saved} state bits removed)"
                      for r in elig]))
    elif roms:
        steps.append(Step("rom_to_case", "NOT_APPLICABLE",
                          "Memories exist but none is a read-only table the converter can prove constant.",
                          evidence=[f"{r.module}.{r.name}: {r.reason}" for r in roms]))
    else:
        steps.append(Step("rom_to_case", "NOT_APPLICABLE", "No constant lookup table (ROM) in the design."))

    compound = [(p, decompose_assertion(str(p.get("expr", "")))) for p in asserts]
    compound = [(p, parts) for p, parts in compound if len(parts) > 1]
    if compound:
        steps.append(Step(
            "assertion_decomposition", "RECOMMENDED",
            "A compound assertion is split into exactly equivalent parts that are proven separately: "
            "cheaper proofs, and a failure names the part that broke.",
            when="before the first proof", soundness="EXACT", apply_with={"decompose": True},
            evidence=[f"{p.get('name')}: {len(parts)} parts" for p, parts in compound]))
    elif asserts:
        steps.append(Step("assertion_decomposition", "NOT_NEEDED",
                          "None of the supplied assertions is a conjunction or a guarded conjunction."))
    else:
        steps.append(Step("assertion_decomposition", "NOT_APPLICABLE", "No assertions were supplied."))

    # 2. invariants --------------------------------------------------------------
    if not facts["clocked"]:
        steps.append(Step("helper_invariants", "NOT_APPLICABLE",
                          "Combinational design: a single-step proof is already exhaustive."))
    elif facts["internal_registers"] == 0:
        steps.append(Step("helper_invariants", "NOT_APPLICABLE", "No internal state to relate."))
    else:
        steps.append(Step(
            "helper_invariants", "IF_STUCK",
            "PDR already searches for an inductive invariant, so mining rarely helps a proof that "
            "PDR closes. It is worth trying when induction fails on a property that is true "
            "(an unreachable state satisfies the property now and breaks it next).",
            when="after an UNKNOWN / induction failure", soundness="EXACT (no behaviors added or removed)",
            apply_with={"auto_invariants": True},
            caveats=["Mining can miss the invariant a proof needs; 'none found' does not mean none exist."]))

    # 3. abstractions that keep PROVEN meaningful -------------------------------------
    counters = find_counter_candidates(mod, rtl_source)
    counter_names = {c.signal for c in counters}
    if counters:
        deep = max(c.thresholds[-1] for c in counters if c.thresholds)
        decision = "RECOMMENDED" if (deep >= 1000 and goal in ("find_bugs", "explore")) else "IF_STUCK"
        steps.append(Step(
            "counter_abstraction", decision,
            "A wide counter compared against large values forces a deep sequential proof; the "
            "abstraction lets it jump to the values the design cares about while still counting "
            "normally in between.",
            when="before a cover/bug hunt, or after a timeout" if decision == "RECOMMENDED" else "after a timeout",
            soundness="SOUND for PROVEN; FALSIFIED may be an artifact",
            apply_with={"counter_abstraction": ",".join(c.signal for c in counters)},
            evidence=[c.reason for c in counters], caveats=[n for c in counters for n in c.notes]))
    else:
        steps.append(Step("counter_abstraction", "NOT_APPLICABLE",
                          "No wide counter compared against a critical value."))

    cuts = [c for c in recommend_cutpoint_candidates(mod, rtl_source) if c.signal not in counter_names]
    if cuts:
        steps.append(Step(
            "cut_points", "RECOMMENDED" if size == "LARGE" else "IF_STUCK",
            "A wide accumulator or arithmetic result feeds a status bit: freeing it removes the "
            "logic that only exists to compute it.",
            when="after a timeout", soundness="SOUND for PROVEN; FALSIFIED may be an artifact",
            apply_with={"cut_signals": ",".join(c.signal for c in cuts[:3])},
            evidence=[f"{c.signal} ({c.width} bits): {c.detail}" for c in cuts[:3]],
            caveats=["Do not cut a signal the property is about: a freed signal can take values its real "
                     "logic never produces."]))
    else:
        steps.append(Step("cut_points", "NOT_APPLICABLE",
                          "No wide counter-free accumulator or arithmetic signal worth freeing."))

    if not facts["hierarchical"]:
        steps.append(Step("black_boxing", "NOT_APPLICABLE",
                          "Single module: there is no submodule to replace."))
    else:
        cands = recommend_blackbox_candidates(mod, rtl_source)
        if cands:
            steps.append(Step(
                "black_boxing", "RECOMMENDED" if size == "LARGE" else "IF_STUCK",
                "A submodule with large memory or wide arithmetic sits in the logic the proof must "
                "reason about; replacing it with free outputs removes it entirely.",
                when="after a timeout", soundness="SOUND for PROVEN; FALSIFIED may be an artifact",
                apply_with={"blackbox_modules": cands[0].module_name},
                evidence=[f"{c.module_name}: {c.reason}, {c.detail}" for c in cands[:3]],
                caveats=["Only black-box a module whose computation the property does not depend on."]))
        else:
            steps.append(Step("black_boxing", "NOT_NEEDED",
                              "The submodules are small and carry no large memory or wide arithmetic.",
                              evidence=[f"submodules: {facts['submodules']}"]))

    # 4. data width ------------------------------------------------------------------
    data = data_signals or guess_data_signals(mod)
    if not data:
        steps.append(Step("data_width_reduction", "NOT_APPLICABLE", "No payload-like input to treat as data."))
    else:
        rep, recs = recommend_data_width_reductions(mod, rtl_source, data)
        if rep.status != "INDEPENDENT":
            steps.append(Step(
                "data_width_reduction", "NOT_APPLICABLE",
                f"Data independence is {rep.status}: data reaches control or the analysis cannot see "
                "through a construct, so a narrower datapath would not be evidence for the real one.",
                evidence=[f"{v.signal} -> {v.sink} (line {v.line})" for v in rep.violations[:4]]
                + [str(u) for u in rep.unknowns[:2]]))
        elif not recs:
            steps.append(Step("data_width_reduction", "NOT_APPLICABLE",
                              "Data-independent, but no literal parameter sizes only data signals."))
        else:
            arith = [p for p in asserts
                     if property_data_use(str(p.get("expr", "")), rep.tainted)[1]]
            mention = [p for p in asserts if property_data_use(str(p.get("expr", "")), rep.tainted)[0]]
            if arith:
                steps.append(Step(
                    "data_width_reduction", "NOT_ACCEPTABLE_FOR_GOAL",
                    "A supplied property applies arithmetic or magnitude comparison to data, which does "
                    "not generalize across widths.", evidence=[str(p.get("name")) for p in arith]))
            else:
                exact = not mention
                steps.append(Step(
                    "data_width_reduction", "RECOMMENDED" if (size == "LARGE" and exact) else "IF_STUCK",
                    "Data cannot reach control, so a control property is exact at a reduced data width."
                    if exact else
                    "Data cannot reach control; the supplied property mentions data only by copy or "
                    "equality, which carries across widths.",
                    when="after a timeout", soundness="EXACT for properties that do not mention data",
                    apply_with={"data_signals": ",".join(rep.data_signals), "data_width_reduction": True},
                    evidence=[f"{r.name}: {r.original} -> {r.proposed}" for r in recs]))

    # 5. bounded configuration --------------------------------------------------------
    prs = recommend_parameter_reductions(rtl_source, mod.name)
    if not prs:
        steps.append(Step("parameter_reduction", "NOT_APPLICABLE",
                          "No structure-named literal parameter of 16 or more (depth, stages, entries...)."))
    elif rigorous:
        steps.append(Step(
            "parameter_reduction", "NOT_ACCEPTABLE_FOR_GOAL",
            "PROVEN would hold only for the reduced configuration, not the real one, so it cannot support "
            "a proof or sign-off. It is still useful to find bugs quickly.",
            soundness="BOUNDED CONFIGURATION", evidence=[f"{r.name}: {r.original} -> {r.proposed}" for r in prs],
            apply_with={"param_overrides": ",".join(f"{r.name}={r.proposed}" for r in prs)}))
    else:
        steps.append(Step(
            "parameter_reduction", "RECOMMENDED" if size != "SMALL" else "NOT_NEEDED",
            "Structural parameters are shrunk so full/empty/wrap corners are reached at a low bound; "
            "bugs found this way usually carry over to the real size.",
            when="first, for bug hunting", soundness="BOUNDED CONFIGURATION; FALSIFIED usually carries over",
            apply_with={"param_overrides": ",".join(f"{r.name}={r.proposed}" for r in prs)},
            evidence=[f"{r.name}: {r.original} -> {r.proposed}" for r in prs]))

    # 6. case split --------------------------------------------------------------------
    if facts["opcode_like_inputs"] and size != "SMALL":
        steps.append(Step(
            "case_split", "IF_STUCK",
            "A wide opcode/mode input partitions the behaviour; one proof per value is easier than one "
            "over all of them.", when="after a timeout",
            soundness="UNSAFE unless the cases are proven to cover the input space",
            evidence=[f"input {n}" for n in facts["opcode_like_inputs"]],
            apply_with={"endpoint": "/api/formal/case_split"},
            caveats=["Always run the completeness check: a forgotten value is the usual failure."]))
    elif facts["opcode_like_inputs"]:
        steps.append(Step("case_split", "NOT_NEEDED", "The design is small enough to prove without splitting."))
    else:
        steps.append(Step("case_split", "NOT_APPLICABLE", "No opcode/mode-like input to split on."))

    # 7. checks around a proof ----------------------------------------------------------
    if assumes:
        steps.append(Step(
            "assumption_necessity", "AFTER_PROOF",
            "Every assumption removes behaviours; re-prove without each to find the ones the proof does "
            "not need and to see exactly what the needed ones exclude.",
            soundness="EXACT", apply_with={"check_assumptions": True},
            evidence=[str(p.get("name")) for p in assumes]))
    else:
        steps.append(Step("assumption_necessity", "NOT_APPLICABLE", "No assumptions supplied."))

    if facts["request_like_inputs"] and facts["response_like_outputs"] and facts["clocked"]:
        steps.append(Step(
            "progress_check", "RECOMMENDED" if goal == "progress" else "OPTIONAL",
            "Request/response-like signals exist. A passing safety proof says nothing about whether "
            "requests are ever answered; the bounded-response check does, and names a stall.",
            when="alongside the safety proof", soundness="EXACT at the stated bound",
            apply_with={"endpoint": "/api/formal/progress"},
            evidence=[f"request-like: {facts['request_like_inputs']}",
                      f"response-like: {facts['response_like_outputs']}"],
            caveats=["Which fairness assumptions are justified, and whether a withdrawn request counts as "
                     "waiting, are protocol facts only you can supply."]))
    elif goal == "progress":
        steps.append(Step("progress_check", "NOT_APPLICABLE",
                          "No request/response-like signal pair was found by name; name them yourself."))
    else:
        steps.append(Step("progress_check", "NOT_APPLICABLE", "No request/response-like signal pair."))

    if not asserts:
        steps.append(Step(
            "assertion_generation", "RECOMMENDED",
            "No assertions were supplied, so there is nothing to prove yet. Generate candidates from the "
            "design (and specification), then let the solver sort them." + (
                "" if has_spec else " A specification text makes the candidates traceable."),
            apply_with={"endpoint": "/api/formal/assertion_search"}, soundness="candidates only; each is "
            "proven or falsified by a solver"))
    else:
        steps.append(Step("assertion_generation", "NOT_NEEDED", "Assertions were supplied."))

    if goal == "signoff":
        steps.append(Step(
            "property_strength_checks", "AFTER_PROOF",
            "A proof is only as good as its properties: check vacuity, signal coverage and whether the "
            "properties kill injected faults.", soundness="EXACT"))
    else:
        steps.append(Step("property_strength_checks", "NOT_NEEDED",
                          "Not a sign-off goal; vacuity is still checked automatically on every PROVEN."))

    return Plan(module=mod.name, goal=goal, facts=facts, steps=steps,
                summary=_summary(facts, steps, goal), notes=_notes(goal))


def _summary(facts: Dict[str, object], steps: List[Step], goal: str) -> str:
    gen = next((s for s in steps if s.technique == "assertion_generation"), None)
    now = [s.technique for s in steps if s.decision == "RECOMMENDED" and s.technique != "assertion_generation"]
    reserve = [s.technique for s in steps if s.decision == "IF_STUCK"]
    after = [s.technique for s in steps if s.decision == "AFTER_PROOF"]
    na = [s.technique for s in steps if s.decision in ("NOT_APPLICABLE", "NOT_NEEDED")]
    no = [s.technique for s in steps if s.decision == "NOT_ACCEPTABLE_FOR_GOAL"]
    parts = [f"Design: {facts['size_class'].lower()}, about {facts['state_bits_estimate']} state bits, "
             f"{'hierarchical' if facts['hierarchical'] else 'single module'}."]
    if gen is not None and gen.decision == "RECOMMENDED":
        parts.append("No assertions were supplied: generate candidates first, then prove them.")
    if not now and not reserve:
        parts.append("A plain proof is the whole plan; no abstraction is warranted.")
    else:
        if now:
            parts.append("Apply now: " + ", ".join(now) + ".")
        if reserve:
            parts.append("Hold in reserve for an inconclusive result: " + ", ".join(reserve) + ".")
        if len(now) + len(reserve) > 1:
            parts.append("More than one technique is plausible; apply them one at a time and stop at the "
                         "first definitive result, so the answer stays attributable.")
    if no:
        parts.append("Not valid for this goal: " + ", ".join(no) + ".")
    if after:
        parts.append("After a proof: " + ", ".join(after) + ".")
    parts.append(f"Skipped as irrelevant here: {len(na)} technique(s).")
    return " ".join(parts)


def _notes(goal: str) -> List[str]:
    notes = ["This is a recommendation, produced without running a solver. Detection is textual, so a "
             "structure hidden behind a macro or generate block can be missed: name signals yourself to "
             "override a skip."]
    if goal == "find_bugs":
        notes.append("Bug hunting: a FALSIFIED under an abstraction must be checked against the real "
                     "design before it is reported as a bug.")
    if goal in ("prove", "signoff"):
        notes.append("Proof goal: only techniques whose PROVEN carries over to the real design are "
                     "recommended; bounded-configuration results are excluded.")
    return notes
