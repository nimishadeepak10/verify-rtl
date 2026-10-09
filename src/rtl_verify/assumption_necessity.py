"""Assumption necessity: which of the supplied assumptions did a proof need?

`assumption_check.py` asks whether the assumption set is self-consistent.
This module asks the opposite, equally important question about a PROVEN
property: is each assumption actually carrying weight?

Why it matters. Every assumption removes behaviors from the proof, so every
assumption is a place a real bug can hide. The MESI work in this project is a
worked example: a mutual-exclusion assumption written to keep the proof clean
turned out to rule out a legitimate scenario (two caches reading in the same
cycle), found only when a counterexample was read by hand. The formal
literature calls the general problem over-constraint and treats it as more
dangerous than under-constraint because it fails silently (Seligman et al.,
"Formal Verification", on assumptions as the unreviewed part of a proof;
Oski Technology's constraint-minimization work in Siemens' Verification
Horizons, already cited in assumption_check.py). Two useful facts follow:

  - An assumption the proof does not need can simply be deleted, shrinking
    the unreviewed surface for free.
  - An assumption the proof does need deserves a human look at exactly what
    it excludes, and the counterexample that appears when it is removed shows
    that directly.

Method (exact, greedy). Start from the full assumption set, under which the
property is PROVEN. For each assumption in turn, re-prove the property with
that one removed from the CURRENT set:
  - PASS: not needed given the rest; drop it permanently.
  - FAIL: needed; keep it, and record the counterexample (the behavior only
    this assumption excludes).
  - anything else (timeout/unknown): undetermined; kept, since redundancy was
    not shown.
What remains is a proof core: a subset that still proves the property and
from which no single assumption can be removed (subset-minimal; not
necessarily the smallest such set).

What the result does and does not say.
  - "Needed" is exact: a real counterexample exists without it.
  - "Not needed" means not needed GIVEN THE OTHERS KEPT. Two assumptions that
    each independently exclude the same bad behavior are both individually
    removable, but not both at once; whichever is tried first is the one
    reported redundant. The order is the order supplied, and the result says
    so.
  - Nothing here judges whether a needed assumption is REASONABLE. That is
    the reader's call, which is why the counterexample is returned.
  - Cost is one proof per assumption per property, so it runs on request and
    is capped.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .analyzer import RtlModule
from .formal_props import generate_formal_wrapper, recommended_engine_chain
from .waveform import vcd_to_json

Property = Tuple[str, str, str]

MAX_RUNS_PER_PROPERTY = 16
_MAX_TRACE_POINTS = 12


@dataclass
class AssumptionRole:
    name: str
    expr: str
    role: str                       # NEEDED | NOT_NEEDED_GIVEN_REST | UNDETERMINED | NOT_CHECKED
    excluded_behavior: Optional[Dict[str, List[Tuple[int, str]]]] = None  # signal -> [(time, value)]
    note: str = ""


@dataclass
class NecessityReport:
    property_name: str
    status: str                     # OK | NOT_APPLICABLE | INCONCLUSIVE
    roles: List[AssumptionRole] = field(default_factory=list)
    runs: int = 0
    note: str = ""

    @property
    def proof_core(self) -> List[str]:
        return [r.name for r in self.roles if r.role in ("NEEDED", "UNDETERMINED", "NOT_CHECKED")]

    @property
    def not_needed(self) -> List[str]:
        return [r.name for r in self.roles if r.role == "NOT_NEEDED_GIVEN_REST"]

    def view(self) -> dict:
        return {
            "property": self.property_name,
            "status": self.status,
            "proof_core": self.proof_core,
            "not_needed_given_the_rest": self.not_needed,
            "runs": self.runs,
            "note": self.note,
            "assumptions": [
                {"name": r.name, "expr": r.expr, "role": r.role,
                 "excluded_behavior": r.excluded_behavior, "note": r.note}
                for r in self.roles
            ],
        }


def _prove(module: RtlModule, rtl_path: Path, backend, props: Sequence[Property], timeout_sec: int,
           depth_override: int, work_dir: Path, engine_kwargs: Optional[dict]):
    chain = recommended_engine_chain(module, kind="assert", depth_override=depth_override)
    per_attempt = max(30, timeout_sec // len(chain))
    work_dir.mkdir(parents=True, exist_ok=True)
    wrapper = work_dir / "wrapper.sv"
    wrapper.write_text(generate_formal_wrapper(module, list(props)), encoding="utf-8")
    result = None
    for i, cfg in enumerate(chain):
        result = backend.run(rtl_path, wrapper, work_dir / f"engine_{i}", top=f"{module.name}_formal_top",
                             depth=cfg["depth"], mode=cfg["mode"], engine=cfg["engine"],
                             timeout_sec=per_attempt, **(engine_kwargs or {}))
        if result.status in ("PASS", "FAIL"):
            break
    return result


def _behavior(module: RtlModule, vcd_path: Optional[Path], expr: str) -> Optional[Dict[str, List[Tuple[int, str]]]]:
    """The counterexample's values of the signals the assumption mentions."""
    if vcd_path is None:
        return None
    wf = vcd_to_json(vcd_path, module=module)
    if "error" in wf:
        return None
    mentioned = set(re.findall(r"[A-Za-z_]\w*", expr))
    out: Dict[str, List[Tuple[int, str]]] = {}
    for sig in wf["signals"]:
        base = sig["name"].split(".")[-1]
        if base in mentioned and base not in out:
            tr = [(t["time"], t["value"]) for t in sig["transitions"]]
            out[base] = tr[-_MAX_TRACE_POINTS:]
    return out or None


def check_assumption_necessity(
    module: RtlModule,
    rtl_path: Path,
    backend,
    assume_props: Sequence[Property],
    prop: Tuple[str, str],
    timeout_sec: int = 90,
    depth_override: int = 0,
    work_root: Optional[Path] = None,
    fixed_assumes: Sequence[Property] = (),
    engine_kwargs: Optional[dict] = None,
    max_runs: int = MAX_RUNS_PER_PROPERTY,
) -> NecessityReport:
    """Greedy proof-core extraction for one PROVEN assert.

    `fixed_assumes` (e.g. proven helper invariants) stay in every run and are
    not themselves questioned. `engine_kwargs` carries manual cut points so
    the runs reproduce the setup the verdict came from.
    """
    name, expr = prop
    report = NecessityReport(property_name=name, status="OK")
    if not assume_props:
        report.status = "NOT_APPLICABLE"
        report.note = "no assumptions were supplied, so there is nothing to question"
        return report
    work_root = work_root or Path(tempfile.mkdtemp(prefix="assume_necessity_"))
    current = list(assume_props)
    roles: Dict[str, AssumptionRole] = {n: AssumptionRole(name=n, expr=e, role="NOT_CHECKED")
                                        for n, e, _k in assume_props}

    for idx, (aname, aexpr, _kind) in enumerate(list(assume_props)):
        if report.runs >= max_runs:
            for rest, _e, _k in list(assume_props)[idx:]:
                roles[rest].note = f"not checked: {max_runs}-run cap reached; kept in the core conservatively"
            break
        trial = [a for a in current if a[0] != aname]
        report.runs += 1
        res = _prove(module, rtl_path, backend, list(fixed_assumes) + trial + [(name, expr, "assert")],
                     timeout_sec, depth_override, work_root / f"drop_{idx}_{aname}", engine_kwargs)
        role = roles[aname]
        if res.status == "PASS":
            role.role = "NOT_NEEDED_GIVEN_REST"
            role.note = "the property is still PROVEN with this removed (given the assumptions kept)"
            current = trial
        elif res.status == "FAIL":
            role.role = "NEEDED"
            role.excluded_behavior = _behavior(module, res.vcd_path, aexpr)
            role.note = ("removing it yields a real counterexample; the values of the signals it "
                         "mentions in that trace show the behavior only this assumption excludes")
        else:
            role.role = "UNDETERMINED"
            role.note = f"the run without it ended {res.status}; redundancy was not shown, so it is kept"

    report.roles = [roles[n] for n, _e, _k in assume_props]
    if any(r.role == "NOT_CHECKED" for r in report.roles):
        report.status = "INCONCLUSIVE"
        report.note = f"stopped after {report.runs} runs; remaining assumptions were not questioned"
    else:
        report.note = ("assumptions were tried in the order supplied; 'not needed' means not needed given "
                       "the others kept (two assumptions excluding the same behavior can each be dropped, "
                       "but not both)")
    return report


def summarize(reports: Sequence[NecessityReport], assume_props: Sequence[Property]) -> dict:
    """Across-property view: which assumptions no checked proof needed."""
    needed_by: Dict[str, List[str]] = {n: [] for n, _e, _k in assume_props}
    checked = [r for r in reports if r.status in ("OK", "INCONCLUSIVE")]
    for r in checked:
        for n in r.proof_core:
            needed_by.setdefault(n, []).append(r.property_name)
    never = [n for n, who in needed_by.items() if not who] if checked else []
    return {
        "properties_checked": [r.property_name for r in checked],
        "needed_by": needed_by,
        "needed_by_no_checked_property": never,
        "note": ("Assumptions in needed_by_no_checked_property were not needed by any checked PROVEN "
                 "property, given the others kept. They can be deleted without weakening those proofs; "
                 "an assumption meant for a property that was FALSIFIED, inconclusive or not checked "
                 "may still matter there.") if checked else "no PROVEN property qualified for the check",
        "per_property": [r.view() for r in reports],
    }
