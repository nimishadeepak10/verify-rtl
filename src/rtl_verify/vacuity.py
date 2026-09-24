"""Automatic vacuity checking for assert-kind formal properties.

An assert can be reported PROVEN for a hollow reason: its own triggering
condition never actually occurs, so the "proof" says nothing about
whatever the property was meant to guarantee. Classic example: `assert(a
-> b)` where `a` can never be true is trivially PROVEN and completely
useless. This project's properties are essentially always written in
exactly that guard/implication shape — every helper in `formal_props.py`
and every hand-written property in the RVFI/MESI scripts follows
`!(GUARD) || (CONCLUSION)`, and `property_to_sva.py`'s own conversion
prompt tells the LLM to write "if A then B" this same way — so vacuity
can be checked automatically and generically, not just for the
LLM-suggested properties that already carry a hand-written
`paired_cover` (see `property_suggester.py` and api/main.py's existing
paired-cover cross-check).

This module adds the fallback that check covers even when no paired
cover was supplied, or wasn't confirmed reachable: extract GUARD from the
property's own expression text, and ask the solver directly whether
GUARD is reachable at all (`cover(GUARD)`). If it's provably never
reachable, the assert is flagged VACUOUS regardless of what its PASS
verdict said. If GUARD isn't extractable (the property isn't in this
shape), the check honestly reports NOT_APPLICABLE rather than guessing.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .analyzer import RtlModule, _find_matching_paren  # same-package helper reuse
from .formal_props import generate_formal_wrapper, recommended_engine_chain


@dataclass
class VacuityCheckResult:
    checked: bool
    method: Optional[str]  # "auto_guard" or None
    guard_expr: Optional[str]
    status: str  # NON_VACUOUS | VACUOUS | UNKNOWN | NOT_APPLICABLE | ERROR
    note: str


def extract_implication_guard(expr: str) -> Optional[str]:
    """Pull GUARD out of `!(GUARD) || (...)`  -- this project's standard
    property shape (see module docstring). Returns None for any
    expression not in exactly this form; deliberately conservative rather
    than guessing at looser patterns, since a wrong guard would make the
    vacuity check itself untrustworthy.
    """
    text = expr.strip()
    if not text.startswith("!(") :
        return None
    open_idx = 1  # index of the '(' right after '!'
    try:
        close_idx = _find_matching_paren(text, open_idx)
    except (ValueError, IndexError):
        return None
    if close_idx < 0 or close_idx >= len(text):
        return None
    guard = text[open_idx + 1 : close_idx].strip()
    rest = text[close_idx + 1 :].strip()
    if not rest.startswith("||") or not guard:
        return None
    return guard


def run_vacuity_check(
    module: RtlModule,
    rtl_path: Path,
    backend,
    name: str,
    expr: str,
    timeout_sec: int = 90,
    depth_override: int = 0,
    work_root: Optional[Path] = None,
) -> VacuityCheckResult:
    """Automatically check whether an assert-kind property is vacuous.

    Extracts GUARD from `expr` and, if found, runs a real `cover(GUARD)`
    through the same engine/solver chain formal properties already use —
    this is a genuine solver call, not a heuristic, so its verdict is as
    trustworthy as any other cover result in this pipeline (and just as
    capable of coming back UNKNOWN/TIMEOUT rather than a false certainty).
    """
    guard = extract_implication_guard(expr)
    if guard is None:
        return VacuityCheckResult(
            checked=False, method=None, guard_expr=None, status="NOT_APPLICABLE",
            note=(
                "Property is not in the !(guard) || (conclusion) implication shape "
                "this automatic check recognizes -- vacuity was not checked."
            ),
        )

    work_root = work_root or Path(tempfile.mkdtemp(prefix=f"vacuity_{name}_"))
    cover_name = f"{name}_vacuity_guard_reachable"
    try:
        wrapper_sv = generate_formal_wrapper(module, [(cover_name, guard, "cover")])
    except ValueError as e:
        return VacuityCheckResult(
            checked=False, method="auto_guard", guard_expr=guard, status="ERROR",
            note=f"Could not build a cover wrapper for the extracted guard: {e}",
        )

    chain = recommended_engine_chain(module, kind="cover", depth_override=depth_override)
    per_attempt_timeout = max(30, timeout_sec // len(chain))
    result = None
    for i, config in enumerate(chain):
        attempt_dir = work_root / f"engine_{i}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        wrapper_path = attempt_dir / "wrapper.sv"
        wrapper_path.write_text(wrapper_sv, encoding="utf-8")
        result = backend.run(
            rtl_path, wrapper_path, attempt_dir,
            top=f"{module.name}_formal_top",
            depth=config["depth"], mode=config["mode"], engine=config["engine"],
            timeout_sec=per_attempt_timeout,
        )
        if result.status in ("PASS", "FAIL"):
            break

    if result is None:
        return VacuityCheckResult(
            checked=False, method="auto_guard", guard_expr=guard, status="ERROR",
            note="Vacuity check produced no result (empty engine chain).",
        )
    if result.status == "PASS":
        return VacuityCheckResult(
            checked=True, method="auto_guard", guard_expr=guard, status="NON_VACUOUS",
            note=f"Confirmed reachable: cover({guard}) REACHED. This assert is not vacuous.",
        )
    if result.status == "FAIL":
        return VacuityCheckResult(
            checked=True, method="auto_guard", guard_expr=guard, status="VACUOUS",
            note=(
                f"cover({guard}) is UNREACHED -- this assert's own triggering condition "
                "never occurs, so its PROVEN verdict holds for a hollow reason, not because "
                "the guarded conclusion was actually exercised. Treat this PROVEN result with "
                "low confidence until the guard (or the property itself) is revisited."
            ),
        )
    return VacuityCheckResult(
        checked=True, method="auto_guard", guard_expr=guard, status="UNKNOWN",
        note=(
            f"cover({guard}) reachability could not be determined ({result.status}) -- "
            "whether this assert is vacuous is unresolved, not confirmed either way."
        ),
    )
