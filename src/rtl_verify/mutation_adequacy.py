"""Property-set adequacy scoring via RTL mutation testing.

Answers a real, previously-unanswered question in this project: "how many
properties is enough, and are they actually any good?" Not with a fixed
number (no such number is meaningful across designs), but with a
measured signal, following YosysHQ's own published MCY (Mutation Cover
with Yosys) methodology: introduce small, real mutations into the RTL
and check whether the EXISTING, already-PROVEN property set notices.
A property set that lets a real, syntactically-valid change to the
design slip through completely undetected has a genuine, measurable gap.

This is a deliberately SIMPLIFIED version of that idea, not the full MCY
tool -- see `mutate.py`'s own module docstring for exactly what's
different (real MCY's separate equivalence-check layer, which this
project doesn't have, would filter out behaviorally-inert mutations
before scoring; without it, an uncaught mutant here might be a real gap
OR a harmless, equivalent change, and this module says so explicitly on
every report rather than implying more precision than it has).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .analyzer import RtlModule
from .formal_props import generate_formal_wrapper, recommended_engine_chain
from .mutate import Mutant, generate_mutants


@dataclass
class MutantResult:
    mutant_id: str
    operator: str
    location: str
    verdict: str  # CAUGHT | NOT_CAUGHT | INCONCLUSIVE
    caught_by: Optional[str]
    detail: str


@dataclass
class MutationAdequacyReport:
    checked: bool
    total_mutants: int = 0
    caught: int = 0
    not_caught: int = 0
    inconclusive: int = 0
    kill_rate: Optional[float] = None
    mutants: List[MutantResult] = field(default_factory=list)
    note: str = ""


def run_mutation_adequacy(
    module: RtlModule,
    rtl_source: str,
    proven_properties: List[Tuple[str, str, str]],
    assume_props: List[Tuple[str, str, str]],
    backend,
    max_mutants: int = 20,
    per_attempt_timeout_sec: int = 30,
    depth_override: int = 0,
    work_root: Optional[Path] = None,
) -> MutationAdequacyReport:
    """Score `proven_properties` (already confirmed PROVEN on the real,
    unmutated design -- checking mutation coverage against a property
    that doesn't even hold on the real design is meaningless) against
    mutants of `module`'s own body.

    For each mutant, tries each proven property in turn against ONLY the
    fastest engine in `recommended_engine_chain()` (PDR for a sequential
    assert), stopping at the first one that FAILS on the mutant (CAUGHT
    -- the property set is sensitive to this class of change). Uses a
    single engine per attempt, not the full multi-engine fallback chain
    every other property check in this project uses, as a deliberate
    cost bound: mutation testing already multiplies solver runs by the
    mutant count, and multiplying again by a full fallback chain per
    mutant would make this prohibitively expensive for anything beyond
    a handful of mutants. A mutant where every property either PASSES
    cleanly or errors/times out, with none FAILING, is NOT_CAUGHT if all
    resolved cleanly, or INCONCLUSIVE if at least one didn't resolve --
    both are reported, never silently folded into the other.
    """
    if not proven_properties:
        return MutationAdequacyReport(
            checked=False,
            note="No properties were confirmed PROVEN on the real design in this run -- "
                 "mutation adequacy needs at least one already-proven property to score against.",
        )

    mutants = generate_mutants(module, rtl_source, max_mutants=max_mutants)
    if not mutants:
        return MutationAdequacyReport(
            checked=False,
            note="No mutable operator occurrences (relational/logical/bitwise/arithmetic) were "
                 "found in this module's own body -- nothing to mutate.",
        )

    work_root = work_root or Path(tempfile.mkdtemp(prefix="mutation_adequacy_"))

    # Every proven property's wrapper is the same across every mutant --
    # build each ONCE up front rather than regenerating it per mutant.
    prop_wrappers: List[Tuple[str, str]] = []
    for name, expr, kind in proven_properties:
        try:
            wrapper_sv = generate_formal_wrapper(module, list(assume_props) + [(name, expr, kind)])
        except ValueError:
            continue
        prop_wrappers.append((name, wrapper_sv))
    if not prop_wrappers:
        return MutationAdequacyReport(
            checked=False,
            note="None of the supplied proven properties could be built into a wrapper -- "
                 "mutation adequacy was not run.",
        )

    chain = recommended_engine_chain(module, kind="assert", depth_override=depth_override)
    fast_config = chain[0]  # deliberately only the first, fastest engine -- see docstring

    results: List[MutantResult] = []
    caught_n = 0
    not_caught_n = 0
    inconclusive_n = 0

    for mutant in mutants:
        mutant_dir = work_root / mutant.id
        mutant_dir.mkdir(parents=True, exist_ok=True)
        mutant_rtl_path = mutant_dir / "dut.v"
        mutant_rtl_path.write_text(mutant.mutated_source, encoding="utf-8")

        caught_by = None
        any_inconclusive = False
        for prop_name, wrapper_sv in prop_wrappers:
            attempt_dir = mutant_dir / prop_name
            attempt_dir.mkdir(parents=True, exist_ok=True)
            wrapper_path = attempt_dir / "wrapper.sv"
            wrapper_path.write_text(wrapper_sv, encoding="utf-8")
            result = backend.run(
                mutant_rtl_path, wrapper_path, attempt_dir,
                top=f"{module.name}_formal_top",
                depth=fast_config["depth"], mode=fast_config["mode"], engine=fast_config["engine"],
                timeout_sec=per_attempt_timeout_sec,
            )
            if result.status == "FAIL":
                caught_by = prop_name
                break
            if result.status != "PASS":
                any_inconclusive = True

        if caught_by is not None:
            verdict = "CAUGHT"
            caught_n += 1
            detail = f"'{caught_by}' failed against this mutant -- the property set detects this change."
        elif any_inconclusive:
            verdict = "INCONCLUSIVE"
            inconclusive_n += 1
            detail = "At least one property could not be resolved (ERROR/TIMEOUT/UNKNOWN) against " \
                      "this mutant, and none failed -- whether the property set would catch this " \
                      "change is unresolved, not confirmed either way."
        else:
            verdict = "NOT_CAUGHT"
            not_caught_n += 1
            detail = (
                "Every property still PASSED against this mutant -- either a real gap in the "
                "property set, or this specific change happens to be behaviorally equivalent to "
                "the original (dead code, a redundant recomputation). This module has no "
                "equivalence-checking layer to tell the two apart (see its own module docstring) "
                "-- treat this as a candidate to investigate by hand, not a confirmed bug."
            )

        results.append(MutantResult(
            mutant_id=mutant.id, operator=mutant.operator, location=mutant.location,
            verdict=verdict, caught_by=caught_by, detail=detail,
        ))

    scored = caught_n + not_caught_n
    kill_rate = (caught_n / scored) if scored > 0 else None
    return MutationAdequacyReport(
        checked=True,
        total_mutants=len(mutants),
        caught=caught_n, not_caught=not_caught_n, inconclusive=inconclusive_n,
        kill_rate=kill_rate,
        mutants=results,
        note=(
            f"{caught_n}/{len(mutants)} mutants caught"
            + (f" ({kill_rate:.0%} of the {scored} mutants that resolved cleanly)" if scored else "")
            + f", {not_caught_n} not caught, {inconclusive_n} inconclusive. "
            "A NOT_CAUGHT mutant may be a real property-set gap or a behaviorally-equivalent "
            "change this module can't distinguish (no netlist-level equivalence checking) -- "
            "review each one's `location` by hand before treating it as a confirmed gap."
        ),
    )
