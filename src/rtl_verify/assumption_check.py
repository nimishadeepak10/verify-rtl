"""Assumption consistency ("over-constraint") checking for formal runs.

Researched before writing any code, the same way this project's other
formal-methodology features were: an over-constrained assumption set is
one of the most consistently documented real failure modes in formal
verification, arguably more dangerous than an under-constrained one,
because it fails silently. EDN's own FPV methodology write-up puts it
plainly: "Conflicting constraints can be seen as the most extreme form
of an over-constrained environment... so constrained that there are no
legal inputs, meaning that no assertions can fail, in effect because no
checking is done." Every assert in that state is vacuously PROVEN for a
completely hollow reason (this project's existing `vacuity.py` catches a
per-assert version of this same failure mode -- a guard that never
triggers -- but a genuinely inconsistent *assumption set* poisons every
property in the run at once, not just one).

The check this module runs is the natural sibling to `vacuity.py`: where
`vacuity.py` asks "is this assert's own guard reachable at all?",
`check_assumption_consistency()` asks "is there ANY reachable state at
all once every supplied assumption is applied together?" -- a real
`cover(1)` under the full assumption set, not a heuristic. If it comes
back unreachable, the assumption set is genuinely self-contradictory
(or contradicts the design), and every PROVEN/UNREACHED verdict in that
same run is suspect, exactly as EDN's guidance describes.

When that happens, this module isolates WHICH assumption(s) are
responsible via minimal-subset extraction (delta-debugging: iteratively
drop one assumption, re-check reachability, keep the drop only if the
set is STILL contradictory without it) -- the same idea Oski
Technology's Anshul Jain describes in Verification Horizons ("Minimizing
Constraints to Debug Vacuous Proofs": isolating a minimal set of
constraints necessary for a vacuous/failing cover, rather than leaving
an engineer to manually bisect a long assumption list by hand).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .analyzer import RtlModule
from .formal_props import generate_formal_wrapper, recommended_engine_chain

# Bounds the number of extra solver calls minimal-subset isolation can
# cost: naive pairwise delta-debugging is O(n^2) solver runs in the
# worst case. Above this many assumptions, isolation is skipped
# (reported honestly, not silently) rather than let one inconsistency
# check balloon into dozens of proof attempts.
_MAX_ASSUMPTIONS_FOR_ISOLATION = 8


@dataclass
class AssumptionConsistencyResult:
    checked: bool
    status: str  # CONSISTENT | OVER_CONSTRAINED | UNKNOWN | NOT_APPLICABLE | ERROR
    note: str
    minimal_conflicting_set: Optional[List[str]] = field(default=None)


def _reachability_status(
    module: RtlModule,
    rtl_path: Path,
    backend,
    assume_props: List[Tuple[str, str, str]],
    timeout_sec: int,
    depth_override: int,
    work_dir: Path,
) -> str:
    """Real `cover(1)` under exactly this assumption set -- PASS means
    reachable (consistent so far), FAIL means no legal input scenario
    exists under these assumptions together (over-constrained)."""
    try:
        wrapper_sv = generate_formal_wrapper(module, list(assume_props) + [("_assumption_reachability", "1", "cover")])
    except ValueError:
        return "ERROR"
    chain = recommended_engine_chain(module, kind="cover", depth_override=depth_override)
    per_attempt_timeout = max(30, timeout_sec // len(chain))
    work_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = work_dir / "wrapper.sv"
    wrapper_path.write_text(wrapper_sv, encoding="utf-8")
    result = None
    for i, config in enumerate(chain):
        attempt_dir = work_dir / f"engine_{i}"
        result = backend.run(
            rtl_path, wrapper_path, attempt_dir,
            top=f"{module.name}_formal_top",
            depth=config["depth"], mode=config["mode"], engine=config["engine"],
            timeout_sec=per_attempt_timeout,
        )
        if result.status in ("PASS", "FAIL"):
            break
    return result.status if result is not None else "ERROR"


def check_assumption_consistency(
    module: RtlModule,
    rtl_path: Path,
    backend,
    assume_props: List[Tuple[str, str, str]],
    timeout_sec: int = 90,
    depth_override: int = 0,
    work_root: Optional[Path] = None,
) -> AssumptionConsistencyResult:
    """Check whether the supplied assumption set is jointly satisfiable
    (a real `cover(1)` reachability run, not a heuristic), and if not,
    isolate a minimal conflicting subset via delta-debugging.
    """
    if not assume_props:
        return AssumptionConsistencyResult(
            checked=False, status="NOT_APPLICABLE",
            note="No assumptions were supplied for this run -- nothing to check.",
        )

    work_root = work_root or Path(tempfile.mkdtemp(prefix="assume_consistency_"))
    status = _reachability_status(
        module, rtl_path, backend, assume_props, timeout_sec, depth_override, work_root / "full",
    )
    if status == "PASS":
        return AssumptionConsistencyResult(
            checked=True, status="CONSISTENT",
            note=(
                f"cover(1) REACHED with all {len(assume_props)} assumption(s) applied together -- "
                "the assumption set permits at least one legal input scenario. This does not by "
                "itself prove the constraints are the RIGHT ones (an under-constrained set is a "
                "separate, opposite risk this check can't detect), only that they aren't "
                "self-contradictory."
            ),
        )
    if status != "FAIL":
        return AssumptionConsistencyResult(
            checked=True, status="UNKNOWN",
            note=f"Could not determine whether the assumption set is jointly satisfiable ({status}).",
        )

    # OVER_CONSTRAINED: no legal input scenario exists under this
    # assumption set. Every PROVEN/UNREACHED verdict from a run that
    # shared these assumptions is suspect for exactly the reason EDN's
    # FPV guidance describes -- surface which assumption(s) are the
    # actual culprit rather than leaving that to manual bisection.
    if len(assume_props) > _MAX_ASSUMPTIONS_FOR_ISOLATION:
        return AssumptionConsistencyResult(
            checked=True, status="OVER_CONSTRAINED",
            note=(
                f"cover(1) is UNREACHED with all {len(assume_props)} assumptions applied together "
                "-- no legal input scenario exists, so every assert in this run would be vacuously "
                "PROVEN and every cover vacuously UNREACHED, not a real result. Minimal-conflicting-"
                f"set isolation was skipped ({len(assume_props)} assumptions exceeds the "
                f"{_MAX_ASSUMPTIONS_FOR_ISOLATION}-assumption bound on extra solver calls this "
                "costs) -- review the full assumption list manually, or resubmit fewer at once."
            ),
        )

    culprits = list(assume_props)
    changed = True
    trial_n = 0
    while changed and len(culprits) > 1:
        changed = False
        for i in range(len(culprits)):
            trial = culprits[:i] + culprits[i + 1:]
            trial_n += 1
            trial_status = _reachability_status(
                module, rtl_path, backend, trial, timeout_sec, depth_override,
                work_root / f"trial_{trial_n}",
            )
            if trial_status == "FAIL":
                # Still contradictory without this one -- it wasn't
                # essential to the conflict, so drop it and keep looking
                # for a smaller culprit set.
                culprits = trial
                changed = True
                break
            # PASS or inconclusive without it: this assumption is part
            # of what's necessary for the conflict -- keep it and try
            # dropping a different one next.

    culprit_names = [name for name, _, _ in culprits]
    return AssumptionConsistencyResult(
        checked=True, status="OVER_CONSTRAINED",
        note=(
            f"cover(1) is UNREACHED with all {len(assume_props)} assumption(s) applied together -- "
            "no legal input scenario exists under this assumption set, so every assert in this run "
            "would be vacuously PROVEN and every cover vacuously UNREACHED, not a real result. "
            f"Isolated via delta-debugging to a minimal conflicting subset: {culprit_names} "
            f"({'this single assumption contradicts the design on its own' if len(culprit_names) == 1 else 'these assumptions are mutually contradictory, or contradict the design, only in combination'})."
        ),
        minimal_conflicting_set=culprit_names,
    )
