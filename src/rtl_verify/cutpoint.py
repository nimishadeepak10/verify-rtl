"""Cut points: free one internal signal, instead of a whole submodule.

Adapted from the abstraction toolkit in Seligman et al.'s "Formal
Verification" (Ch. 10, "Dealing with complexity"), Siemens' Verification
Horizons "How to Reduce the Complexity of Formal Analysis" series, and the
FVM complexity-reduction guide, which all describe the same primitive: a
cut point drops the logic that drives one signal and lets the solver pick
any value for it at every cycle, so everything that only exists to compute
that signal (a 64-bit counter's carry chain, a multiplier feeding a status
bit) stops being part of the proof. `blackbox.py` already does this for a
whole submodule; a cut point is the finer-grained tool for logic that is
NOT a separate module.

Soundness, which every result carries: freeing a signal only ever adds
behaviors, so a PROVEN verdict under a cut point still holds for the real
design (the proof covered a superset). A FALSIFIED verdict may be an
artifact -- the freed signal can now take a value the real logic could
never produce -- so its trace has to be checked against the real driver
before being treated as a bug. Same directional caveat as black-boxing.

Implementation lives in the backend (`backends/symbiyosys.py`, `cutpoints=`
argument) using yosys's own `cutpoint` pass rather than a source rewrite:
a source rewrite cannot reliably separate a register's reads from its
writes without a real parser, and this project's analyzer is text-based.

The candidate ranking here is a static heuristic over the module's own
text, not a dependency analysis: it looks for the two structures the
sources agree make formal engines struggle -- wide free-running counters
and wide arithmetic results -- and ranks by declared width. It never
proposes the FSM state register or the clock/reset, since freeing those
destroys the control behavior almost every property is about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .analyzer import RtlModule
from .blackbox import _find_module_span

_IDENT = re.compile(r"^[A-Za-z_]\w*$")
_DECL = re.compile(
    r"\b(?:reg|wire|logic|output\s+reg|output\s+wire|output\s+logic)\s*(?:signed\s*)?"
    r"(?:\[\s*(\d+)\s*:\s*(\d+)\s*\])?\s*(\w+)"
)


@dataclass
class CutpointCandidate:
    signal: str
    reason: str  # "wide_counter" | "wide_arithmetic"
    detail: str
    width: int


def _declared_widths(body: str) -> dict[str, Optional[int]]:
    widths: dict[str, Optional[int]] = {}
    for m in _DECL.finditer(body):
        hi, lo, name = m.group(1), m.group(2), m.group(3)
        widths.setdefault(name, abs(int(hi) - int(lo)) + 1 if hi is not None else 1)
    return widths


def _target_body(rtl_source: str, module: RtlModule) -> str:
    start, end = _find_module_span(rtl_source, module.name)
    return rtl_source[start:end]


def validate_cutpoints(rtl_source: str, module: RtlModule, signals: List[str]) -> Tuple[List[str], List[str]]:
    """Split `signals` into (usable, error messages). A usable cut point is
    a signal declared in the target module that isn't the clock/reset and
    isn't a bare input (an input is already free).
    """
    body = _target_body(rtl_source, module)
    declared = _declared_widths(body)
    input_names = {p.name for p in module.inputs}
    ok: List[str] = []
    errors: List[str] = []
    for sig in signals:
        if not _IDENT.match(sig):
            errors.append(f"'{sig}' is not a plain signal name")
        elif sig in (module.clock_port, module.reset_port):
            errors.append(f"'{sig}' is the clock/reset; freeing it would make every proof meaningless")
        elif sig in input_names:
            errors.append(f"'{sig}' is an input port and is already unconstrained")
        elif sig not in declared:
            errors.append(f"'{sig}' is not declared in module '{module.name}'")
        else:
            ok.append(sig)
    return ok, errors


def recommend_cutpoint_candidates(module: RtlModule, rtl_source: str, min_width: int = 8) -> List[CutpointCandidate]:
    body = _target_body(rtl_source, module)
    widths = _declared_widths(body)
    skip = {module.clock_port, module.reset_port, module.state_reg}
    found: dict[str, CutpointCandidate] = {}

    for m in re.finditer(r"\b(\w+)\s*<=\s*\1\s*[+-]\s*[^;]+;", body):
        sig = m.group(1)
        w = widths.get(sig) or 0
        if sig in skip or w < min_width:
            continue
        found[sig] = CutpointCandidate(
            sig, "wide_counter",
            f"{w}-bit register updated as `{sig} <= {sig} +/- ...` (counter/accumulator): "
            "reaching its interesting values can take up to 2^width cycles of sequential depth.", w)

    for m in re.finditer(r"(?:assign\s+)?\b(\w+)\s*(?:<=|=)\s*[^;=]*?[A-Za-z0-9_\]\)]\s*[*/%]\s*[A-Za-z0-9_\(][^;]*;", body):
        sig = m.group(1)
        w = widths.get(sig) or 0
        if sig in skip or w < min_width or sig in found:
            continue
        found[sig] = CutpointCandidate(
            sig, "wide_arithmetic",
            f"{w}-bit signal computed with multiply/divide/modulo: wide arithmetic is a "
            "documented hard case for bit-level SAT/BDD engines.", w)

    return sorted(found.values(), key=lambda c: c.width, reverse=True)
