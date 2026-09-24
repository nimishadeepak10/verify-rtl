"""Independent second-solver cross-checking for formal verdicts.

`recommended_engine_chain()` (formal_props.py) already returns genuinely
different (algorithm, solver) pairs -- PDR, k-induction/yices,
k-induction/z3, and boolector for covers. Today those are used purely as
a FALLBACK: try the next rung only if the current one is inconclusive
(TIMEOUT/UNKNOWN/ERROR), stopping at the first real PASS/FAIL. That means
a PROVEN, FALSIFIED, REACHED, or UNREACHED verdict comes from exactly one
engine's opinion -- if that specific engine has a tool-specific bug (this
project found two, in a single session, in yosys's own SystemVerilog
frontend -- see docs/systemverilog_ieee1800_rules.md and
docs/verification_plan_mesi.md), nothing else in the pipeline would catch
it.

This module adds active cross-verification: once a primary verdict is
reached, re-run the SAME property against the NEXT untried chain rung (a
genuinely different algorithm and/or SMT solver -- yices, z3, boolector,
PDR are all independent implementations from different teams) to
completion, and compare. Agreement is real evidence the verdict isn't a
tool-specific artifact; disagreement is flagged as a serious anomaly
needing manual review -- never silently resolved by trusting either side.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .analyzer import RtlModule
from .formal_props import generate_formal_wrapper, recommended_engine_chain


@dataclass
class CrossCheckResult:
    performed: bool
    engine_label: Optional[str]
    status: Optional[str]  # raw backend status: PASS/FAIL/TIMEOUT/UNKNOWN/ERROR/CANCELLED
    agrees: Optional[bool]  # None when not performed or itself inconclusive
    note: str


def cross_check_property(
    module: RtlModule,
    rtl_path: Path,
    backend,
    name: str,
    expr: str,
    kind: str,
    primary_success: bool,
    primary_engine_label: str,
    timeout_sec: int = 90,
    depth_override: int = 0,
    work_root: Optional[Path] = None,
) -> CrossCheckResult:
    """Re-run `expr` against an independent engine and compare.

    Picks the first chain rung whose label differs from
    `primary_engine_label` -- not just "the next entry in the list", since
    the primary run may have already fallen back past one or more
    inconclusive attempts before reaching its verdict. If the design's
    chain has only one rung (a combinational design, where a single BMC
    step is already SAT-exhaustive -- see recommended_engine_chain's own
    docstring), there is no independent second engine to run; this is
    reported honestly via `performed=False`, not silently skipped.
    """
    chain = recommended_engine_chain(module, kind=kind, depth_override=depth_override)
    candidates = [c for c in chain if c["label"] != primary_engine_label]
    if not candidates:
        return CrossCheckResult(
            performed=False, engine_label=None, status=None, agrees=None,
            note=(
                "No independent second engine available for this design/kind "
                "(the chain has only one genuinely different rung)."
            ),
        )
    cross_config = candidates[0]

    try:
        wrapper_sv = generate_formal_wrapper(module, [(name, expr, kind)])
    except ValueError as e:
        return CrossCheckResult(
            performed=False, engine_label=cross_config["label"], status=None, agrees=None,
            note=f"Could not build a cross-check wrapper: {e}",
        )

    work_root = work_root or Path(tempfile.mkdtemp(prefix=f"crosscheck_{name}_"))
    work_root.mkdir(parents=True, exist_ok=True)
    wrapper_path = work_root / "wrapper.sv"
    wrapper_path.write_text(wrapper_sv, encoding="utf-8")

    result = backend.run(
        rtl_path, wrapper_path, work_root,
        top=f"{module.name}_formal_top",
        depth=cross_config["depth"], mode=cross_config["mode"], engine=cross_config["engine"],
        timeout_sec=timeout_sec,
    )

    if result.status not in ("PASS", "FAIL"):
        return CrossCheckResult(
            performed=True, engine_label=cross_config["label"], status=result.status, agrees=None,
            note=(
                f"Cross-check with {cross_config['label']} was inconclusive ({result.status}) — "
                "agreement with the primary verdict is not established either way."
            ),
        )

    agrees = result.success == primary_success
    if agrees:
        note = (
            f"Independently confirmed by {cross_config['label']} (a genuinely different "
            "engine/solver from the primary run) — agrees with the primary verdict."
        )
    else:
        note = (
            f"DISAGREEMENT: the primary engine says "
            f"{'PASS' if primary_success else 'FAIL'}, {cross_config['label']} says "
            f"{'PASS' if result.success else 'FAIL'}. This needs manual review — either one "
            "engine has a real bug on this specific property, or one caught a genuine subtlety "
            "the other missed. Never silently trust either side over the other."
        )
    return CrossCheckResult(
        performed=True, engine_label=cross_config["label"], status=result.status,
        agrees=agrees, note=note,
    )
