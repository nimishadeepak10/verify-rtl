"""A language-model technique selector, as the comparison point for the rule
based planner.

The model is shown the design, the properties and the technique catalogue and
asked for an ordered list of techniques with their parameters. The chosen
techniques then run through the SAME executor as the planner's, so the only
difference between the two strategies is who selected, and the token cost of
selecting is measured by `llm_client`'s counters.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from rtl_verify import llm_client
from rtl_verify.strategy_planner import Plan, Step

CATALOGUE = {
    "rom_to_case": "replace a constant lookup table by a case statement (exact)",
    "assertion_decomposition": "split a conjunctive assertion into parts (exact)",
    "helper_invariants": "mine and prove helper invariants for induction (exact)",
    "counter_abstraction": "let a wide counter jump to the values it is compared with; needs `signals`",
    "black_boxing": "replace a submodule by free outputs; needs `signals` = module names",
    "cut_points": "free a wide internal signal; needs `signals`",
    "data_width_reduction": "shrink data width when data never reaches control; needs `signals` = data inputs",
    "parameter_reduction": "shrink structural parameters; needs `params` as NAME=VALUE",
}
SOUND = {
    "rom_to_case": "EXACT", "assertion_decomposition": "EXACT", "helper_invariants": "EXACT",
    "counter_abstraction": "SOUND for PROVEN; FALSIFIED may be an artifact",
    "black_boxing": "SOUND for PROVEN; FALSIFIED may be an artifact",
    "cut_points": "SOUND for PROVEN; FALSIFIED may be an artifact",
    "data_width_reduction": "EXACT for properties that do not mention data",
    "parameter_reduction": "BOUNDED CONFIGURATION",
}
SCHEMA = {
    "type": "object",
    "properties": {"techniques": {"type": "array", "items": {
        "type": "object",
        "properties": {"technique": {"type": "string", "enum": list(CATALOGUE)},
                       "signals": {"type": "array", "items": {"type": "string"}},
                       "params": {"type": "array", "items": {"type": "string"}}},
        "required": ["technique", "signals", "params"], "additionalProperties": False}}},
    "required": ["techniques"], "additionalProperties": False,
}
SYSTEM = ("You advise a formal verification engineer. Given an RTL design and properties, choose which "
          "complexity-reduction techniques to try, in order, to prove them with a model checker. Choose only "
          "techniques the design actually has structure for, and give the exact signal or module names the "
          "technique needs. Return an empty list if the plain proof is enough.")


def _apply(tech: str, signals: List[str], params: List[str]) -> Dict[str, object]:
    j = ",".join(signals)
    return {
        "helper_invariants": {"auto_invariants": True},
        "counter_abstraction": {"counter_abstraction": j},
        "black_boxing": {"blackbox_modules": j},
        "cut_points": {"cut_signals": j},
        "data_width_reduction": {"data_signals": j, "data_width_reduction": True},
        "parameter_reduction": {"param_overrides": ",".join(params)},
        "rom_to_case": {"rom_to_case": True},
        "assertion_decomposition": {"decompose": True},
    }[tech]


def select(rtl: str, top: str, props: List[dict], goal: str = "prove") -> Tuple[Plan, List[str]]:
    catalogue = "\n".join(f"- {k}: {v}" for k, v in CATALOGUE.items())
    user = (f"Goal: {goal}\nTop module: {top}\n\nTechniques:\n{catalogue}\n\nProperties:\n"
            + "\n".join(f"- {p['name']}: {p['expr']}" for p in props if p.get("kind") != "assume")
            + f"\n\nRTL:\n{rtl[:6000]}")
    out = llm_client.complete_structured(SYSTEM, user, SCHEMA, max_tokens=800)
    steps: List[Step] = [Step("plain_proof", "RUN_FIRST", "baseline", soundness="EXACT")]
    order: List[str] = []
    for item in out.get("techniques", []):
        tech = item["technique"]
        decision = "RECOMMENDED" if tech in ("rom_to_case", "assertion_decomposition") else "IF_STUCK"
        steps.append(Step(tech, decision, "selected by the model", soundness=SOUND[tech],
                          apply_with=_apply(tech, item.get("signals", []), item.get("params", []))))
        if decision == "IF_STUCK":
            order.append(tech)
    plan = Plan(module=top, goal=goal, facts={}, steps=steps, summary="llm-selected")
    return plan, order
