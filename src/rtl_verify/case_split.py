"""Case-split completeness: when a proof is split into several runs, each
restricted by a case assumption, prove the cases together cover everything.

Case-splitting is one of the standard complexity techniques (Seligman et
al., "Formal Verification", Ch. 10; Siemens Verification Horizons; FVM
complexity guide): instead of one hard proof over every opcode, run one
easier proof per opcode range. The same book's Ch. 9 ("Formal verification's
greatest bloopers") is blunt about its one real danger -- unlike black-
boxing or cut points, case-splitting is UNSAFE, because each case adds an
assumption, and nothing checks that the cases add up to the whole space.
Two of its real post-mortems are exactly this:

  - an arithmetic block with a 6-bit exponent was split into 0..31 and
    33..64; the forgotten value 32, a power of two and the one risky case,
    was the one that had a real bug, found much later;
  - a PCIe block was "verified" in x8 mode only; the changes touched logic
    shared with x4 and x16, and a bug was found once those were run.

Both passed review because every individual run was green. This module
makes the gap a solver result instead of a review comment:

  1. COMPLETENESS (exact): under the global assumptions, assert
     `case_1 || case_2 || ...`. PROVEN means no legal input escapes every
     case; FALSIFIED means it does, and the counterexample's values of the
     signals the cases mention are the uncovered example (the "32").
  2. NON-VACUITY (exact): per case, `cover(case_i)` under the global
     assumptions. A case the global assumptions already rule out would
     "pass" its proofs for the empty reason the book's vacuity cases warn
     about.
  3. SEQUENCE CAVEAT (exact where decidable): the completeness check is per
     cycle. If a case is expressed over a signal that can change from cycle
     to cycle, every run keeps it inside ONE case for its whole length, so
     behaviors that mix cases across time belong to no run, even when every
     single cycle is covered (the book's own warning that cross-products
     of cases can still need a run on the full model). Each input signal
     named in a case is checked for `x == $past(x)` under the global
     assumptions: all stable means the split is sequence-safe; otherwise
     the result says exactly which signals vary.
  4. The real per-case runs: each supplied assert is proven once per case
     with that case's expression added as an assumption.

What it does not do: it cannot tell whether the cases are the RIGHT cases
for the design's risk (e.g. whether the boundary values the designer
worried about are in their own cases) -- only that nothing is missing and
nothing is empty.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .analyzer import PortDirection, RtlModule
from .formal_props import generate_formal_wrapper, recommended_engine_chain
from .waveform import vcd_to_json

Property = Tuple[str, str, str]


@dataclass
class CaseOutcome:
    name: str
    expr: str
    reachable: Optional[bool]  # None = undecided
    reachable_note: str
    property_verdicts: Dict[str, str] = field(default_factory=dict)  # prop -> PROVEN/FALSIFIED/INCONCLUSIVE


@dataclass
class CaseSplitReport:
    completeness: str  # COMPLETE | GAP | UNKNOWN | ERROR
    uncovered_example: Optional[Dict[str, int]]
    completeness_note: str
    sequence_safety: str  # SEQUENCE_SAFE | PER_CYCLE_ONLY | UNKNOWN | NOT_APPLICABLE
    varying_signals: List[str]
    sequence_note: str
    cases: List[CaseOutcome]
    verdict: str  # SPLIT_PROVEN | SPLIT_FALSIFIED | SPLIT_INCOMPLETE | SPLIT_INCONCLUSIVE
    issues: List[str]


def _run(module, rtl_path: Path, backend, props: List[Property], kind: str,
         depth_override: int, timeout_sec: int, work: Path):
    wrapper = generate_formal_wrapper(module, props)
    chain = recommended_engine_chain(module, kind=kind, depth_override=depth_override)
    per = max(30, timeout_sec // len(chain))
    work.mkdir(parents=True, exist_ok=True)
    wpath = work / "wrapper.sv"
    wpath.write_text(wrapper, encoding="utf-8")
    result = None
    for i, cfg in enumerate(chain):
        attempt = work / f"engine_{i}"
        result = backend.run(
            rtl_path, wpath, attempt, top=f"{module.name}_formal_top",
            depth=cfg["depth"], mode=cfg["mode"], engine=cfg["engine"], timeout_sec=per,
        )
        if result.status in ("PASS", "FAIL"):
            break
    return result


def _identifiers(expr: str) -> List[str]:
    seen, out = set(), []
    for tok in re.findall(r"\$?[A-Za-z_]\w*", expr):
        if tok.startswith("$") or tok in seen or re.fullmatch(r"\d+", tok):
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _final_values(vcd_path: Optional[Path], module: RtlModule, names: List[str]) -> Dict[str, int]:
    if vcd_path is None:
        return {}
    wf = vcd_to_json(vcd_path, module=module)
    if "error" in wf:
        return {}
    out: Dict[str, int] = {}
    for sig in wf["signals"]:
        short = sig["name"].split(".")[-1].split("[")[0]
        if short in names and short not in out and sig["transitions"]:
            val = sig["transitions"][-1]["value"]
            if re.fullmatch(r"[01]+", val):
                out[short] = int(val, 2)
    return out


def check_case_split(
    module: RtlModule,
    rtl_path: Path,
    backend,
    cases: List[Tuple[str, str]],
    global_assumes: List[Property],
    properties: List[Property],
    timeout_sec: int = 120,
    depth_override: int = 0,
    work_root: Optional[Path] = None,
) -> CaseSplitReport:
    work_root = work_root or Path(tempfile.mkdtemp(prefix="case_split_"))
    issues: List[str] = []
    port_names = {p.name for p in module.ports}
    input_names = {p.name for p in module.ports if p.direction == PortDirection.INPUT}
    skip = {module.clock_port, module.reset_port}
    case_idents: List[str] = []
    for _n, e in cases:
        for ident in _identifiers(e):
            if ident in port_names and ident not in case_idents:
                case_idents.append(ident)

    # 1. completeness
    union = " || ".join(f"({e})" for _n, e in cases)
    try:
        comp = _run(module, rtl_path, backend, list(global_assumes) + [("cases_cover_everything", union, "assert")],
                    "assert", depth_override, timeout_sec, work_root / "completeness")
    except ValueError as e:
        return CaseSplitReport("ERROR", None, f"Could not build the completeness check: {e}",
                               "UNKNOWN", [], "", [], "SPLIT_INCONCLUSIVE", [str(e)])
    example: Optional[Dict[str, int]] = None
    if comp.status == "PASS":
        completeness, c_note = "COMPLETE", "No input allowed by the global assumptions escapes every case."
    elif comp.status == "FAIL":
        completeness = "GAP"
        example = _final_values(comp.vcd_path, module, case_idents)
        c_note = ("At least one input allowed by the global assumptions is in NO case, so no run covers it"
                  + (f" -- e.g. {example}." if example else "."))
        issues.append("cases do not cover the full input space")
    else:
        completeness = "UNKNOWN"
        c_note = f"Completeness check did not decide ({comp.status})."
        issues.append("completeness undecided")

    # 2. sequence caveat
    if not module.is_sequential:
        seq, varying, s_note = "NOT_APPLICABLE", [], "Combinational design: no cross-cycle behavior to mix cases across."
    else:
        varying, undecided = [], False
        for ident in case_idents:
            if ident in skip:
                continue
            if ident not in input_names:
                varying.append(ident)  # internal/output state is not held constant by a case assumption
                continue
            try:
                r = _run(module, rtl_path, backend,
                         list(global_assumes) + [(f"{ident}_is_stable", f"{ident} == $past({ident})", "assert")],
                         "assert", depth_override, timeout_sec, work_root / f"stable_{ident}")
            except ValueError:
                undecided = True
                continue
            if r.status == "FAIL":
                varying.append(ident)
            elif r.status != "PASS":
                undecided = True
        if varying:
            seq = "PER_CYCLE_ONLY"
            s_note = (f"{varying} can change from cycle to cycle under the global assumptions. Each run "
                      "keeps it inside one case for its whole length, so behaviors that mix cases across "
                      "time are in no run even though every single cycle is covered. Split on a signal "
                      "held constant (add an assumption that it is stable) or also run the unsplit model.")
            issues.append("split signal varies over time (per-cycle coverage only)")
        elif undecided:
            seq, s_note = "UNKNOWN", "Stability of the split signals could not be decided."
        else:
            seq, s_note = "SEQUENCE_SAFE", "Every signal the cases mention is held constant by the global assumptions."

    # 3 + 4. per case: non-vacuity, then each property
    outcomes: List[CaseOutcome] = []
    any_false = any_inconclusive = False
    for idx, (cname, cexpr) in enumerate(cases):
        d = work_root / f"case_{idx}"
        try:
            r = _run(module, rtl_path, backend, list(global_assumes) + [(f"{cname}_reachable", cexpr, "cover")],
                     "cover", depth_override, timeout_sec, d / "reach")
            reachable = True if r.status == "PASS" else False if r.status == "FAIL" else None
        except ValueError:
            reachable = None
        if reachable is False:
            r_note = "Case is unsatisfiable under the global assumptions: its proofs would pass for no reason."
            issues.append(f"case '{cname}' is empty under the global assumptions")
        elif reachable is None:
            r_note = "Reachability of this case could not be decided."
        else:
            r_note = "Case is reachable."
        outcome = CaseOutcome(cname, cexpr, reachable, r_note)
        for pname, pexpr, _k in properties:
            try:
                pr = _run(module, rtl_path, backend,
                          list(global_assumes) + [(f"{cname}_case", cexpr, "assume"), (pname, pexpr, "assert")],
                          "assert", depth_override, timeout_sec, d / f"prop_{pname}")
                v = "PROVEN" if pr.status == "PASS" else "FALSIFIED" if pr.status == "FAIL" else "INCONCLUSIVE"
            except ValueError:
                v = "INCONCLUSIVE"
            outcome.property_verdicts[pname] = v
            any_false |= v == "FALSIFIED"
            any_inconclusive |= v == "INCONCLUSIVE"
        outcomes.append(outcome)

    if any_false:
        verdict = "SPLIT_FALSIFIED"
        issues.append("a property is FALSIFIED in at least one case")
    elif completeness == "GAP" or any(o.reachable is False for o in outcomes):
        verdict = "SPLIT_INCOMPLETE"
    elif completeness == "UNKNOWN" or any_inconclusive or seq in ("UNKNOWN",) or any(o.reachable is None for o in outcomes):
        verdict = "SPLIT_INCONCLUSIVE"
    elif seq == "PER_CYCLE_ONLY":
        verdict = "SPLIT_PROVEN_PER_CYCLE_ONLY"
    else:
        verdict = "SPLIT_PROVEN"
    return CaseSplitReport(completeness, example, c_note, seq, varying, s_note, outcomes, verdict, issues)
