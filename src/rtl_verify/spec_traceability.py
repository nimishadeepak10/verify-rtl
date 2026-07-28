"""Phase 4: spec -> verification-plan traceability.

Extracts atomic, testable requirements from a design spec, then checks
each one against this design's actual coverage items (the auto-generated
verification plan's test cases, plus any already-verified formal
properties the user supplies) — and honestly flags any requirement with
no genuine match, rather than a false "covered".

This is judgment, not proof: unlike the formal pipeline (Phase 1/2), a
"covered" mapping here is the model's read of whether an existing test
case or property description actually addresses a requirement, not a
machine-checked guarantee. The value is visibility — every requirement
extracted from the spec is either linked to something concrete or
explicitly flagged as unlinked, so nothing silently falls through.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import llm_client
from .analyzer import RtlModule
from .vplan import VerificationPlan

REQUIREMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["requirements"],
    "additionalProperties": False,
}

_EXTRACT_SYSTEM = """You read a hardware design specification and extract atomic, testable \
requirements — statements about behavior that a verification engineer could actually check.

Rules:
- One requirement = one independently testable sentence. If a sentence bundles two claims \
("X and Y"), split it into two requirements — a compound requirement hides which half actually \
got tested.
- Only extract requirements describing verifiable DESIGN BEHAVIOR (signal relationships, timing, \
reset behavior, state transitions, error handling). Skip pure background/documentation text, \
naming conventions, or non-functional prose that isn't itself a checkable claim.
- ID each requirement sequentially: R1, R2, R3, ...
- Write each requirement's "text" as a self-contained sentence — a reader shouldn't need the \
surrounding spec paragraph to understand it.
- If the spec is vague or doesn't specify a concrete design (e.g. only prose with no checkable \
claims), return as few requirements as genuinely testable — do not invent requirements not \
actually stated or implied.
"""


def extract_requirements(spec_text: str, rtl_context: str = "") -> list[dict]:
    user = f"Specification:\n{spec_text.strip()}\n"
    if rtl_context.strip():
        user += f"\n(For context, the RTL this spec describes:)\n```\n{rtl_context.strip()}\n```\n"
    result = llm_client.complete_structured(_EXTRACT_SYSTEM, user, REQUIREMENTS_SCHEMA, max_tokens=3000)
    return result.get("requirements", [])


@dataclass
class CoverageItem:
    id: str
    source: str  # "test_plan" | "property"
    description: str


def coverage_items_from_plan(plan: VerificationPlan) -> list[CoverageItem]:
    """Flatten a VerificationPlan's enabled test cases into candidate
    coverage items, reusing the existing plan model rather than a new one.
    """
    items: list[CoverageItem] = []
    n = 0
    for cat in plan.categories or []:
        if cat.status.value != "enabled":
            continue
        for case in cat.cases or []:
            n += 1
            items.append(CoverageItem(id=f"TC{n}", source="test_plan", description=f"[{cat.name}] {case.description}"))
        for sub in cat.subcategories or []:
            if sub.status.value != "enabled":
                continue
            for case in sub.cases or []:
                n += 1
                items.append(
                    CoverageItem(id=f"TC{n}", source="test_plan", description=f"[{cat.name}/{sub.name}] {case.description}")
                )
    return items


def coverage_items_from_properties(properties_text: str) -> list[CoverageItem]:
    """One property description per non-empty line — the plain-English
    descriptions a user copies over from the Formal tab (Phase 2).
    """
    items: list[CoverageItem] = []
    for i, line in enumerate(l.strip() for l in properties_text.splitlines()):
        if not line:
            continue
        items.append(CoverageItem(id=f"PROP{i + 1}", source="property", description=line))
    return items


MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement_id": {"type": "string"},
                    "covered": {"type": "boolean"},
                    "linked_item_ids": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                },
                "required": ["requirement_id", "covered", "linked_item_ids", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["mappings"],
    "additionalProperties": False,
}

_MAP_SYSTEM = """You are checking traceability between a design's spec requirements and its \
actual verification coverage.

You are given a numbered list of coverage items — concrete test cases from this design's \
verification plan, and/or formal properties (assert/assume/cover) already written for it — each \
with an id and a plain-English description.

For every requirement, decide: does at least one coverage item genuinely, specifically verify \
this exact requirement?
- Set "covered": true and list the matching item id(s) in "linked_item_ids" ONLY if the match is \
real and specific — not a vague topical overlap (e.g. both mention "reset" is not enough; the \
item must actually check the same behavior the requirement describes).
- If nothing in the list actually verifies it, set "covered": false, "linked_item_ids": [], and \
explain in "rationale" what kind of test or property would be needed to close the gap.
- A false "covered" is worse than an honest "untested" — when genuinely unsure, prefer \
"covered": false.
"""


def build_mapping_prompt(requirements: list[dict], items: list[CoverageItem]) -> str:
    reqs_txt = "\n".join(f"  {r['id']}: {r['text']}" for r in requirements)
    items_txt = "\n".join(f"  {it.id} ({it.source}): {it.description}" for it in items) or "  (none provided)"
    return (
        f"Requirements:\n{reqs_txt}\n\n"
        f"Coverage items available for this design:\n{items_txt}\n"
    )


def map_requirements(requirements: list[dict], items: list[CoverageItem]) -> list[dict]:
    if not requirements:
        return []
    user = build_mapping_prompt(requirements, items)
    result = llm_client.complete_structured(_MAP_SYSTEM, user, MAPPING_SCHEMA, max_tokens=4000)
    return result.get("mappings", [])


def build_traceability_matrix(
    spec_text: str,
    rtl_source: str,
    module: RtlModule,
    plan: VerificationPlan | None,
    properties_text: str = "",
) -> dict:
    """The full Phase 4 pipeline: extract requirements, gather coverage
    items from the plan + optional pasted properties, map, and summarize.
    """
    requirements = extract_requirements(spec_text, rtl_context=rtl_source)

    items: list[CoverageItem] = []
    if plan is not None:
        items.extend(coverage_items_from_plan(plan))
    items.extend(coverage_items_from_properties(properties_text))

    mappings = map_requirements(requirements, items)
    mapping_by_id = {m["requirement_id"]: m for m in mappings}
    items_by_id = {it.id: it for it in items}

    rows = []
    gap_count = 0
    for r in requirements:
        m = mapping_by_id.get(r["id"])
        covered = bool(m and m.get("covered"))
        linked_ids = (m.get("linked_item_ids") or []) if m else []
        linked = [
            {"id": lid, "source": items_by_id[lid].source, "description": items_by_id[lid].description}
            for lid in linked_ids
            if lid in items_by_id
        ]
        if not covered:
            gap_count += 1
        rows.append({
            "id": r["id"],
            "text": r["text"],
            "covered": covered,
            "linked_items": linked,
            "rationale": (m.get("rationale") if m else "") or "",
        })

    return {
        "module": module.name,
        "requirements": rows,
        "total": len(rows),
        "gap_count": gap_count,
        "num_coverage_items": len(items),
    }
