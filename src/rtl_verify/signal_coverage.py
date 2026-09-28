"""Cone-of-influence / verified-surface analysis for a property set --
Step 3 of the published "Seven Steps of Formal Signoff" methodology
(SemiEngineering, the OneSpin/Siemens formal verification team): "tracing
backwards from assertions to identify which design elements are
verified, revealing any registers, inputs, or outputs not being checked
by formal analysis."

Researched alongside this project's other formal-methodology features
(vacuity, assumption consistency, mutation adequacy): checking that
published 7-step methodology against what already existed here found
three of the seven steps already independently implemented --
`spec_traceability.py` (step 1, test plan/spec review), `assumption_
check.py` (step 4, over-constraint analysis), `mutation_adequacy.py`
(step 7, mutation testing) -- and this module closes step 3, the one
genuinely missing, implementable piece (steps 2, 5, and 6 -- designer
review, sequential-depth confidence, and solver-internal "formal core"
extraction -- either need a human in the loop or solver-internal
machinery this project doesn't have access to, and aren't claimed here).

This is deliberately a STATIC, textual scan of property expressions
against a module's own port list -- not a real structural cone-of-
influence trace through the RTL's own logic (that would need a real
netlist/dependency graph, which this project's text-based analyzer
doesn't build). A port that's never referenced by name in ANY property
expression is flagged as outside this property set's verified surface;
one that IS referenced might still not be fully constrained by what
references it (this module doesn't claim that level of precision) --
consistent with this project's standing rule to report exactly what was
checked, not to imply a stronger result than what was actually earned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from .analyzer import PortDirection, RtlModule


def _identifiers_in(expr: str) -> Set[str]:
    return set(re.findall(r"\b[A-Za-z_]\w*\b", expr))


@dataclass
class SignalCoverageReport:
    total_ports: int
    covered_ports: List[str] = field(default_factory=list)
    uncovered_ports: List[str] = field(default_factory=list)
    coverage_percent: float = 0.0
    note: str = ""


def analyze_signal_coverage(
    module: RtlModule,
    properties: List[Tuple[str, str, str]],
) -> SignalCoverageReport:
    """Which of `module`'s own ports are referenced by at least one
    property expression, and which are never mentioned by anything in
    this property set at all.

    The clock port is excluded from the universe being scored -- a
    property expression referencing the clock signal itself would be
    unusual and isn't what "verified" means here. Reset IS included: a
    reset port never referenced by any property is worth surfacing even
    though this project's own $past()-history guard already handles
    reset structurally for one-cycle-back claims without a property
    needing to name it explicitly -- see this module's own note field
    for that caveat, since an "uncovered" reset isn't automatically a
    real gap the way an uncovered DATA port usually is.
    """
    referenced: Set[str] = set()
    for _name, expr, _kind in properties:
        referenced |= _identifiers_in(expr)

    scored_ports = [p for p in module.ports if p.name != module.clock_port]
    covered = [p.name for p in scored_ports if p.name in referenced]
    uncovered = [p.name for p in scored_ports if p.name not in referenced]

    total = len(scored_ports)
    pct = (len(covered) / total * 100) if total else 100.0

    note = (
        f"{len(covered)}/{total} port(s) referenced by at least one property "
        f"({pct:.0f}%). This is a textual reference scan, not a real structural "
        "cone-of-influence trace -- a listed port being 'covered' means some "
        "property mentions it by name, not that every bit of its behavior is "
        "fully constrained."
    )
    if module.reset_port and module.reset_port in uncovered:
        note += (
            f" '{module.reset_port}' (reset) is uncovered by name, but this project's "
            "own $past()-history guard already handles reset structurally for every "
            "one-cycle-back property without it needing to appear in the expression "
            "text -- an uncovered reset port is not automatically a real gap the way "
            "an uncovered data port usually is."
        )

    return SignalCoverageReport(
        total_ports=total, covered_ports=covered, uncovered_ports=uncovered,
        coverage_percent=pct, note=note,
    )
