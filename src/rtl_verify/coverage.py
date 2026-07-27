from __future__ import annotations

"""
Code coverage data model + post-simulation computations.

Overall percent weighting:
- Combinational design: average(statement, branch, toggle)
- Sequential design: average(statement, branch, toggle, fsm_state)
- If FSM is not applicable OR has no transitions detected, FSM is dropped from the average.
"""

from dataclasses import dataclass, field
import re
from typing import Dict, List, Optional, Tuple

from .analyzer import RtlModule


@dataclass
class StatementCoverage:
    total: int
    hit: int
    uncovered_lines: List[int]
    hit_counts: Dict[int, int]

    @property
    def percent(self) -> float:
        return 100.0 * self.hit / self.total if self.total else 0.0


@dataclass
class BranchCoverage:
    total: int
    hit: int
    branches: List[dict]

    @property
    def percent(self) -> float:
        return 100.0 * self.hit / self.total if self.total else 0.0


@dataclass
class ToggleCoverage:
    """For every bit of every signal: did it go 0->1 AND 1->0?"""

    total_bits: int
    bits_toggled_up: int
    bits_toggled_down: int
    bits_toggled_both: int
    per_signal: Dict[str, dict]

    @property
    def percent(self) -> float:
        return 100.0 * self.bits_toggled_both / self.total_bits if self.total_bits else 0.0


@dataclass
class FsmCoverage:
    """States visited and transitions exercised (sequential designs only)."""

    state_reg: Optional[str]
    total_states: int
    states_visited: int
    states_hit: List[str]
    states_missed: List[str]
    total_transitions: int
    transitions_taken: int
    transitions_hit: List[tuple]
    transitions_missed: List[tuple]
    not_applicable: bool = False
    na_reason: str = ""

    @property
    def state_percent(self) -> float:
        if self.not_applicable or not self.total_states:
            return 0.0
        return 100.0 * self.states_visited / self.total_states

    @property
    def transition_percent(self) -> float:
        if self.not_applicable or not self.total_transitions:
            return 0.0
        return 100.0 * self.transitions_taken / self.total_transitions

    @classmethod
    def not_applicable_result(cls, reason: str) -> "FsmCoverage":
        return cls(
            state_reg=None,
            total_states=0,
            states_visited=0,
            states_hit=[],
            states_missed=[],
            total_transitions=0,
            transitions_taken=0,
            transitions_hit=[],
            transitions_missed=[],
            not_applicable=True,
            na_reason=reason,
        )


@dataclass
class CoverageReport:
    statement: StatementCoverage
    branch: BranchCoverage
    toggle: ToggleCoverage
    fsm: FsmCoverage
    overall_percent: float
    meta: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "meta": dict(self.meta or {}),
            "overall_percent": float(self.overall_percent),
            "statement": {
                "total": int(self.statement.total),
                "hit": int(self.statement.hit),
                "percent": float(self.statement.percent),
                "uncovered_lines": list(self.statement.uncovered_lines or []),
                "hit_counts": {str(k): int(v) for k, v in (self.statement.hit_counts or {}).items()},
            },
            "branch": {
                "total": int(self.branch.total),
                "hit": int(self.branch.hit),
                "percent": float(self.branch.percent),
                "branches": list(self.branch.branches or []),
            },
            "toggle": {
                "total_bits": int(self.toggle.total_bits),
                "bits_toggled_up": int(self.toggle.bits_toggled_up),
                "bits_toggled_down": int(self.toggle.bits_toggled_down),
                "bits_toggled_both": int(self.toggle.bits_toggled_both),
                "percent": float(self.toggle.percent),
                "per_signal": dict(self.toggle.per_signal or {}),
            },
            "fsm": {
                "state_reg": self.fsm.state_reg,
                "total_states": int(self.fsm.total_states),
                "states_visited": int(self.fsm.states_visited),
                "states_hit": list(self.fsm.states_hit or []),
                "states_missed": list(self.fsm.states_missed or []),
                "total_transitions": int(self.fsm.total_transitions),
                "transitions_taken": int(self.fsm.transitions_taken),
                "transitions_hit": [list(x) for x in (self.fsm.transitions_hit or [])],
                "transitions_missed": [list(x) for x in (self.fsm.transitions_missed or [])],
                "not_applicable": bool(self.fsm.not_applicable),
                "na_reason": self.fsm.na_reason or "",
                "state_percent": float(self.fsm.state_percent),
                "transition_percent": float(self.fsm.transition_percent),
            },
        }


def compute_overall_percent(
    statement: StatementCoverage,
    branch: BranchCoverage,
    toggle: ToggleCoverage,
    fsm: FsmCoverage,
    is_sequential: bool,
) -> float:
    parts: List[float] = []
    if statement.total:
        parts.append(statement.percent)
    if branch.total:
        parts.append(branch.percent)
    if toggle.total_bits:
        parts.append(toggle.percent)
    if is_sequential and not fsm.not_applicable and fsm.total_transitions > 0:
        parts.append(fsm.state_percent)
    return sum(parts) / len(parts) if parts else 0.0


def _transition_values_to_ints(transitions: List[dict]) -> List[Tuple[int, Optional[int], bool]]:
    """
    Convert VCD transitions (time, value string) into (time, int_value_or_None, has_unknown).
    """
    out: List[Tuple[int, Optional[int], bool]] = []
    for tr in transitions or []:
        t = int(tr.get("time", 0))
        v = str(tr.get("value", ""))
        if not v:
            continue
        if any(ch in v.upper() for ch in ("X", "Z", "?")):
            out.append((t, None, True))
            continue
        try:
            out.append((t, int(v, 2), False))
        except ValueError:
            out.append((t, None, True))
    return out


def compute_toggle_coverage(vcd_signals: List[dict], dut_port_names: List[str]) -> ToggleCoverage:
    """
    Given parsed VCD signals (vcd_to_json format), compute per-bit toggle coverage.
    Only counts DUT port signals; excludes testbench/results/reference groups by construction.
    """
    dut_set = set(dut_port_names or [])
    per_signal: Dict[str, dict] = {}
    total_bits = 0
    toggled_up_total = 0
    toggled_down_total = 0
    toggled_both_total = 0

    for sig in vcd_signals or []:
        name = str(sig.get("name", ""))
        if not name or name not in dut_set:
            continue
        width = int(sig.get("width") or 1)
        transitions = _transition_values_to_ints(sig.get("transitions") or [])
        if not transitions:
            continue

        up = [False] * width
        down = [False] * width
        prev: Optional[int] = None
        prev_unknown = True

        for _, cur, cur_unknown in transitions:
            if prev is None or prev_unknown or cur_unknown or cur is None:
                prev = cur
                prev_unknown = cur_unknown
                continue
            for i in range(width):
                pb = (prev >> i) & 1
                nb = (cur >> i) & 1
                if pb == 0 and nb == 1:
                    up[i] = True
                elif pb == 1 and nb == 0:
                    down[i] = True
            prev = cur
            prev_unknown = cur_unknown

        both = [bool(u and d) for u, d in zip(up, down)]
        per_signal[name] = {"width": width, "bits_both": both, "bits_up": up, "bits_down": down}

        total_bits += width
        toggled_up_total += sum(1 for b in up if b)
        toggled_down_total += sum(1 for b in down if b)
        toggled_both_total += sum(1 for b in both if b)

    return ToggleCoverage(
        total_bits=total_bits,
        bits_toggled_up=toggled_up_total,
        bits_toggled_down=toggled_down_total,
        bits_toggled_both=toggled_both_total,
        per_signal=per_signal,
    )


def build_env_timeline_from_vcd_signals(vcd_signals: List[dict]) -> Dict[int, Dict[str, Optional[int]]]:
    """
    Build env_by_time for CoverageInterpreter from vcd_to_json-style signals.
    env_by_time[t][sig] = int value, or None if unknown at that time.
    """
    env_by_time: Dict[int, Dict[str, Optional[int]]] = {}
    latest: Dict[str, Optional[int]] = {}

    # Gather all times.
    times: set[int] = set()
    for sig in vcd_signals or []:
        for tr in sig.get("transitions") or []:
            try:
                times.add(int(tr.get("time", 0)))
            except (TypeError, ValueError):
                continue
    if not times:
        return {}
    ordered = sorted(times)

    # Index transitions per signal for single pass update.
    per_sig = {}
    for sig in vcd_signals or []:
        name = str(sig.get("name", ""))
        if not name:
            continue
        trs = sig.get("transitions") or []
        # ensure time sorted
        try:
            trs2 = sorted(trs, key=lambda x: int(x.get("time", 0)))
        except Exception:
            trs2 = trs
        per_sig[name] = trs2
        latest[name] = None

    idx: Dict[str, int] = {k: 0 for k in per_sig.keys()}
    for t in ordered:
        for name, trs in per_sig.items():
            j = idx[name]
            while j < len(trs) and int(trs[j].get("time", 0)) <= t:
                v = str(trs[j].get("value", "")).upper()
                if not v:
                    latest[name] = None
                elif any(ch in v for ch in ("X", "Z", "?")):
                    latest[name] = None
                else:
                    try:
                        latest[name] = int(v, 2)
                    except ValueError:
                        latest[name] = None
                j += 1
            idx[name] = j
        env_by_time[t] = dict(latest)

    return env_by_time


def compute_fsm_coverage(mod: RtlModule, vcd_signals: List[dict]) -> FsmCoverage:
    if not mod.is_sequential:
        return FsmCoverage.not_applicable_result(
            "Design is combinational — no state register detected"
        )
    if not mod.state_reg:
        return FsmCoverage.not_applicable_result(
            "Sequential design, but no state register detected"
        )

    state_sig = next((s for s in (vcd_signals or []) if s.get("name") == mod.state_reg), None)
    if not state_sig:
        return FsmCoverage.not_applicable_result(
            f"State register '{mod.state_reg}' not present in VCD"
        )

    transitions = _transition_values_to_ints(state_sig.get("transitions") or [])
    if not transitions:
        return FsmCoverage.not_applicable_result(
            f"No transitions for state register '{mod.state_reg}'"
        )

    # Build a decode map. Best-effort: if analyzer captured symbolic states, use those as names.
    # If not, present binary as names.
    decode: Dict[int, str] = {}
    # Prefer literal labels (e.g. 3'b000) from fsm_value_map when possible.
    for k, name in (mod.fsm_value_map or {}).items():
        if re.match(r"^\d+'\s*[bB]\s*[01_]+$", k):
            m = re.match(r"^(\d+)'\s*[bB]\s*([01_]+)$", k)
            if m:
                bits = m.group(2).replace("_", "")
                try:
                    decode[int(bits, 2)] = name
                except ValueError:
                    pass

    states_hit: List[str] = []
    visited: set[str] = set()
    taken_transitions: set[Tuple[str, str]] = set()

    prev_state: Optional[str] = None
    prev_unknown = True
    for _, val, unk in transitions:
        if val is None or unk:
            prev_unknown = True
            continue
        cur = decode.get(val) or str(val)
        visited.add(cur)
        if cur not in states_hit:
            states_hit.append(cur)
        if prev_state is not None and not prev_unknown and cur != prev_state:
            taken_transitions.add((prev_state, cur))
        prev_state = cur
        prev_unknown = False

    all_states = list(mod.states or [])
    total_states = len(all_states) if all_states else len(visited)
    # `visited`/`states_hit` are only in the same vocabulary as `all_states`
    # (mod.states' symbolic names) when `decode` actually mapped runtime
    # int codes to those names. When it's empty — e.g. a state defined via
    # `localparam S_RED = 2'd0;` outside the case body, which
    # fsm_value_map never captures a literal->name mapping for — `visited`
    # holds raw numeric strings instead, and diffing that against symbolic
    # names would falsely report every actually-visited state as missed
    # (confirmed by running traffic_light_fsm.v: states_hit=["0","1"] but
    # states_missed=["S_RED","S_GREEN"] even though both were visited).
    # state_percent still reads correctly either way (it only compares
    # counts, not names) — only this missed-name list needs the guard.
    missed = [s for s in all_states if s not in visited] if (all_states and decode) else []

    all_transitions = set(tuple(x) for x in (mod.fsm_transitions or []))
    total_transitions = len(all_transitions)
    transitions_hit = sorted(taken_transitions)
    transitions_missed = (
        sorted(list(all_transitions - taken_transitions)) if (all_transitions and decode) else []
    )

    return FsmCoverage(
        state_reg=mod.state_reg,
        total_states=total_states,
        states_visited=len(visited),
        states_hit=states_hit,
        states_missed=missed,
        total_transitions=total_transitions,
        transitions_taken=len(taken_transitions),
        transitions_hit=transitions_hit,
        transitions_missed=transitions_missed,
        not_applicable=False,
        na_reason="",
    )

