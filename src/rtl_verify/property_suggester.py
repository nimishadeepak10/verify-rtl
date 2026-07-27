"""LLM-assisted property suggestion: RTL (+ optional spec) -> classified
assert/assume/cover proposals in plain English.

This is Phase 2's core engine. Per direct feedback from formal engineers,
the actual unsolved gap in this space is spec + RTL -> correctly classified
plain-English properties — converting an already-classified plain-English
property into SVA is comparatively commodity. So this module is where the
real value is; see property_to_sva.py for the (deliberately simpler)
conversion step.

Every proposal is grounded in docs/formal_property_reference.md — vetted,
sourced material (Accellera OVL, the Dwyer/Avrunin/Corbett pattern
taxonomy, real open-source formal projects) — rather than the model's raw
pretrained recall.
"""

from __future__ import annotations

from pathlib import Path

from . import llm_client
from .analyzer import RtlModule

_ROOT = Path(__file__).resolve().parents[2]
_REFERENCE_PATH = _ROOT / "docs" / "formal_property_reference.md"

PATTERNS = [
    "Absence", "Existence", "Bounded Existence", "Universality",
    "Precedence", "Response", "Chain Precedence", "Chain Response",
]

SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "properties": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["assert", "assume", "cover"]},
                    "pattern": {"type": "string", "enum": PATTERNS},
                    "description": {"type": "string"},
                    "signals": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                },
                "required": ["name", "kind", "pattern", "description", "signals", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["properties"],
    "additionalProperties": False,
}

_SYSTEM_TEMPLATE = """You are assisting a hardware design verification engineer. Your job is \
NOT to write SystemVerilog assertions yet — it is to read a digital design (RTL) and, if given, \
a specification, and propose a list of properties that should hold, each correctly classified.

Classify every property as exactly one of:
- "assert": something the DESIGN must guarantee — its outputs or internal state.
- "assume": something the ENVIRONMENT/inputs guarantee, which the design is allowed to rely on.
- "cover": a reachability claim — proof that some interesting scenario can actually happen.

Follow this reference material precisely. It reflects real, sourced formal-verification \
practice — an industry standard (Accellera OVL), a well-known academic property-pattern \
taxonomy, and lessons from real open-source formal-verification projects — not generic advice:

<reference>
{reference}
</reference>

Rules:
- Classify by WHO is being constrained: a claim about an input signal's own behavior is \
"assume" (per the "assume the inputs" rule in the reference). A claim about internal state or \
outputs is "assert".
- For any assert whose scenario only applies under specific conditions, also propose a matching \
cover proving that scenario is reachable at all — an assert with no way to confirm it isn't \
vacuous is not useful (see the reference's cover/vacuity lesson).
- Tag each property with the closest Dwyer/Avrunin/Corbett pattern name from the reference.
- Where the design's signal names match a standard OVL checker role (one-hot, handshake/req-ack, \
fifo, mutex), say so explicitly in the rationale.
- Every expression must use the DUT's own port names exactly as given below — never invent a \
signal name that isn't in the port list.
- Only propose properties you can actually justify from the given RTL structure and/or spec \
text. Do not propose generic properties unrelated to this specific design.
- Propose between 3 and 10 properties, prioritizing the most important ones for this design.
- Every field of every property must be fully and specifically written out. Never write \
placeholder, filler, or generic text (e.g. "placeholder", "TBD", "property description here") —
if you cannot fully justify a specific property, propose fewer properties instead.
"""


def load_reference_doc() -> str:
    try:
        return _REFERENCE_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def build_user_prompt(module: RtlModule, rtl_source: str, spec_text: str) -> str:
    ports = "\n".join(
        f"  - {p.name} ({p.direction.value}, width {p.width})" for p in module.ports
    )
    parts = [
        f"Module: {module.name}",
        f"Sequential: {module.is_sequential}",
        f"Clock port: {module.clock_port or '(none — combinational)'}",
        f"Reset port: {module.reset_port or '(none detected)'}",
        "Ports:",
        ports,
        "",
        "RTL source:",
        "```",
        rtl_source,
        "```",
    ]
    if spec_text.strip():
        parts.extend(["", "Specification:", spec_text.strip()])
    else:
        parts.extend(
            [
                "",
                "(No specification was provided — propose properties from the RTL "
                "structure and port/signal naming alone.)",
            ]
        )
    return "\n".join(parts)


def suggest_properties(module: RtlModule, rtl_source: str, spec_text: str = "") -> list[dict]:
    """Return proposed properties: name, kind, pattern, description, signals, rationale."""
    system = _SYSTEM_TEMPLATE.format(reference=load_reference_doc())
    user = build_user_prompt(module, rtl_source, spec_text)
    result = llm_client.complete_structured(system, user, SUGGEST_SCHEMA, max_tokens=6000)
    return result.get("properties", [])
