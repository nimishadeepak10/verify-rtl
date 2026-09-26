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
from .spec_patterns import PATTERNS, SCOPES  # noqa: F401 (PATTERNS re-exported for existing callers)

_ROOT = Path(__file__).resolve().parents[2]
_REFERENCE_PATH = _ROOT / "docs" / "formal_property_reference.md"

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
                    "scope": {"type": "string", "enum": SCOPES},
                    "scope_detail": {"type": "string"},
                    "description": {"type": "string"},
                    "signals": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                    "paired_cover": {"type": "string"},
                },
                "required": [
                    "name", "kind", "pattern", "scope", "scope_detail",
                    "description", "signals", "rationale", "paired_cover",
                ],
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
- Every assert's "paired_cover" field must name exactly which cover property (its "name" field, \
from this same response) proves the assert's triggering condition is reachable. Set it to the \
empty string only if the assert genuinely holds unconditionally (true for every reachable state, \
no triggering scenario to confirm) — never leave it empty just because you forgot to propose the \
cover; propose the cover first, then link it. Cover and assume-kind properties should set \
"paired_cover" to "".
- Tag each property with the closest Dwyer/Avrunin/Corbett pattern name from the reference, AND \
its scope -- the taxonomy's second, independent dimension, for WHERE in execution the pattern \
applies: "Global" (holds throughout, no bounding event), "Before" (up to a named event), "After" \
(from a named event onward), "Between" (from one named event to a second), or "After-Until" (like \
Between, but the window continues even if the second event never happens). Put the actual \
bounding event(s) in "scope_detail" (e.g. "After reset deasserts", "Between grant asserted and \
the corresponding ack", or "" for Global). Most hardware properties are genuinely Global -- don't \
force a narrower scope where none is warranted -- but a real reset-recovery claim, a claim that \
only holds during a specific multi-cycle transaction, or a claim about behavior strictly before \
some one-time event should be tagged with the scope that actually matches it, not flattened to \
Global by default.
- Where the design's signal names match a standard OVL checker role (one-hot, handshake/req-ack, \
fifo, mutex), say so explicitly in the rationale.
- Every expression must use the DUT's own port names exactly as given below — never invent a \
signal name that isn't in the port list.
- A port named `__dbg_<signal>` is a DEBUG PORT: read-only visibility into an internal register \
called `<signal>` that has no real port of its own (added automatically so properties can \
reference internal state — tags, valid bits, FSM/control registers — not just I/O). Treat it as \
an ordinary signal of the DUT for property purposes. If it's paired with a port named \
`__dbgsel_<signal>`, `<signal>` is an internal ARRAY (e.g. a tag/valid table): `__dbgsel_<signal>` \
is an index input selecting which array element `__dbg_<signal>` currently reads. Left \
unconstrained, the solver is free to pick any index every cycle — which is a genuine, useful \
"for every entry" check, not a limitation.
- One-cycle "previous cycle" / "next cycle" claims (using `$past()`) ARE supported by this \
pipeline's conversion step now — propose them when the design genuinely needs one (a value that \
only changes by a bounded amount per cycle, a state that must update exactly one cycle after a \
trigger, and so on). One real correctness requirement when the claim is about a `__dbg_*` ARRAY \
specifically: a property comparing that array's value ACROSS TWO CYCLES must also require the \
`__dbgsel_<signal>` index stayed the SAME across those cycles (e.g. include \
`$past(__dbgsel_x) == __dbgsel_x` in the condition), or the claim silently compares two different \
array entries instead of the same line's value over time — a real bug found and fixed this way \
during this feature's own development, not a hypothetical. Claims reaching back more than one \
cycle, or unbounded/liveness claims ("eventually", "within N cycles"), are still not supported — \
propose those as-is and let the conversion step correctly decline them, rather than not proposing \
a genuinely-needed property just because it might be declined.
- Only propose properties you can actually justify from the given RTL structure and/or spec \
text. Do not propose generic properties unrelated to this specific design.
- There is no target property count and no cap. Propose as many properties as this specific \
design genuinely needs to be meaningfully verified — a simple design may only need a handful; a \
complex one (multiple interacting state machines, arithmetic with edge cases, wide control logic) \
may need dozens. Do not stop early to hit some round number, and do not pad the list with \
generic or redundant properties just to reach one. Every property must earn its place by \
covering something the others don't.
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
    # No property-count cap (see the system prompt above) means no fixed
    # token budget either -- a genuinely complex design proposing dozens of
    # fully-written-out properties needs real headroom, not the old 6000
    # tokens sized for a 3-10-property target. Thinking stays disabled in
    # complete_structured() (see llm_client.py), so this budget goes
    # entirely to the actual JSON output.
    result = llm_client.complete_structured(system, user, SUGGEST_SCHEMA, max_tokens=16000)
    return result.get("properties", [])
