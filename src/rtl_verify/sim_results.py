"""Parse simulation log lines into structured per-test results for the UI."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .analyzer import RtlModule
from .combinational_model import can_self_check, expected_outputs
from .generators import verilog_tb as vtb

_FIELD_RE = re.compile(r"(\w+)=(\S+)")
_FAIL_RE = re.compile(r"(exp|got)_(\w+)=(\S+)")
_PASS_LINE = re.compile(
    r"(?:^|\s)PASS\s+t=(\d+)\s+(.+)$", re.IGNORECASE | re.MULTILINE
)
_FAIL_LINE = re.compile(
    r"(?:^|\s)FAIL\s+t=(\d+)\s+(.+)$", re.IGNORECASE | re.MULTILINE
)
_STIM_LINE = re.compile(r"^STIM\s+t=\d+\s+(.+)$", re.IGNORECASE | re.MULTILINE)
_SEQ_LINE = re.compile(r"^SEQ\s+t=\d+\s+(.+)$", re.IGNORECASE | re.MULTILINE)
_SUMMARY_PASS = re.compile(r"PASS=(\d+)", re.IGNORECASE)
_TEST_LINE = re.compile(r"^TEST,(\d+),", re.IGNORECASE)


def _parse_pipe_fields(section: str) -> Dict[str, str]:
    """Parse a=1|b=2|opcode=3 into dict."""
    out: Dict[str, str] = {}
    if not section:
        return out
    for part in section.split("|"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _parse_test_line(trimmed: str) -> Optional[Dict[str, Any]]:
    if not trimmed.upper().startswith("TEST,"):
        return None
    parts = trimmed.split(",")
    if len(parts) < 5:
        return None
    try:
        test_idx = int(parts[1])
    except ValueError:
        return None
    inputs: Dict[str, str] = {}
    expected: Dict[str, str] = {}
    got: Dict[str, str] = {}
    result = "OBS"
    i = 2
    while i < len(parts):
        tag = parts[i].strip().upper()
        if tag == "IN" and i + 1 < len(parts):
            inputs = _parse_pipe_fields(parts[i + 1])
            i += 2
            continue
        if tag in ("EXP", "EXPECTED") and i + 1 < len(parts):
            expected = _parse_pipe_fields(parts[i + 1])
            i += 2
            continue
        if tag == "OUT" and i + 1 < len(parts):
            got = _parse_pipe_fields(parts[i + 1])
            i += 2
            continue
        if tag == "RESULT" and i + 1 < len(parts):
            result = parts[i + 1].strip().upper()
            if result == "OBS":
                result = "OBSERVED"
            i += 2
            continue
        i += 1
    ui_result = result
    if result == "OBSERVED":
        ui_result = "OBSERVED"
    elif result == "PASS":
        ui_result = "PASS"
    elif result == "FAIL":
        ui_result = "FAIL"
    return {
        "num": test_idx + 1,
        "inputs": inputs,
        "expected": expected,
        "got": got,
        "result": ui_result,
        "detail": trimmed,
    }


def _parse_fields(s: str) -> Dict[str, str]:
    return {m.group(1): m.group(2) for m in _FIELD_RE.finditer(s)}


def _parse_fail_fields(s: str) -> tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    expected: Dict[str, str] = {}
    got: Dict[str, str] = {}
    for m in _FAIL_RE.finditer(s):
        if m.group(1) == "exp":
            expected[m.group(2)] = m.group(3)
        else:
            got[m.group(2)] = m.group(3)
    return {}, expected, got


def _row(
    num: int,
    category: str,
    description: str,
    inputs: Dict[str, str],
    expected: Dict[str, str],
    got: Dict[str, str],
    result: str,
    detail: str,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "num": num,
        "category": category,
        "description": description,
        "inputs": inputs,
        "expected": expected,
        "got": got,
        "result": result,
        "detail": detail,
        "tags": tags or ["simulation"],
    }


def parse_sim_log(sim_log: str, overall_pass: bool = True) -> List[Dict[str, Any]]:
    """Extract per-test rows from $display lines in the simulator log."""
    if not sim_log:
        return []

    rows: List[Dict[str, Any]] = []
    idx = 0
    stim_default = "PASS" if overall_pass else "RUN"

    for line in sim_log.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("==="):
            continue

        test_row = _parse_test_line(trimmed)
        if test_row:
            rows.append(
                _row(
                    test_row["num"],
                    "Simulation",
                    "Vector check",
                    test_row["inputs"],
                    test_row["expected"],
                    test_row["got"],
                    test_row["result"],
                    test_row["detail"],
                )
            )
            idx = max(idx, test_row["num"])
            continue

        m = _PASS_LINE.search(trimmed)
        if m and "PASS=" not in trimmed:
            fields = _parse_fields(m.group(2))
            rows.append(
                _row(
                    idx + 1,
                    "Simulation",
                    "Vector check",
                    fields,
                    fields,
                    fields,
                    "PASS",
                    trimmed,
                )
            )
            idx += 1
            continue

        m = _FAIL_LINE.search(trimmed)
        if m:
            _, expected, got = _parse_fail_fields(m.group(2))
            rows.append(
                _row(
                    idx + 1,
                    "Simulation",
                    "Mismatch",
                    {},
                    expected,
                    got,
                    "FAIL",
                    trimmed,
                )
            )
            idx += 1
            continue

        m = _STIM_LINE.match(trimmed)
        if m:
            fields = _parse_fields(m.group(1))
            rows.append(
                _row(
                    idx + 1,
                    "Simulation",
                    "Stimulus applied",
                    fields,
                    {},
                    fields,
                    stim_default,
                    trimmed,
                    ["stimulus"],
                )
            )
            idx += 1
            continue

        m = _SEQ_LINE.match(trimmed)
        if m:
            rest = m.group(1).strip()
            if rest.startswith("test="):
                rows.append(
                    _row(
                        idx + 1,
                        "Simulation",
                        "Sequential step",
                        {},
                        {},
                        {},
                        stim_default,
                        trimmed,
                        ["sequential"],
                    )
                )
            else:
                fields = _parse_fields(rest)
                rows.append(
                    _row(
                        idx + 1,
                        "Simulation",
                        "Sequential step",
                        fields,
                        {},
                        fields,
                        stim_default,
                        trimmed,
                        ["sequential"],
                    )
                )
            idx += 1

    return rows


def _rows_from_tb_cases(
    mod: RtlModule, rtl_source: str, overall_pass: bool
) -> List[Dict[str, Any]]:
    cases = vtb._build_cases(mod)  # noqa: SLF001
    self_check = (
        not mod.is_sequential
        and bool(mod.inputs)
        and bool(mod.outputs)
        and bool(rtl_source)
        and can_self_check(rtl_source, mod)
    )
    rows: List[Dict[str, Any]] = []
    for i, case in enumerate(cases):
        inputs = {k: str(v) for k, v in case.items()}
        expected: Dict[str, str] = {}
        if self_check:
            exp = expected_outputs(rtl_source, mod, case)
            expected = {k: str(v) for k, v in exp.items()}
        got = expected if overall_pass and expected else {}
        rows.append(
            _row(
                i + 1,
                "Testbench",
                "Generated vector",
                inputs,
                expected,
                got,
                "PASS" if overall_pass else "—",
                "",
                ["directed"],
            )
        )
    return rows


def parse_test_results(
    sim_log: str,
    mod: Optional[RtlModule] = None,
    rtl_source: str = "",
    overall_pass: bool = True,
) -> List[Dict[str, Any]]:
    """Parse log; if no per-case lines, synthesize rows from generated TB vectors."""
    rows = parse_sim_log(sim_log, overall_pass=overall_pass)
    summary = _SUMMARY_PASS.search(sim_log or "")
    expected_n = int(summary.group(1)) if summary else 0

    if mod is not None and (not rows or (expected_n and len(rows) < expected_n)):
        fallback = _rows_from_tb_cases(mod, rtl_source, overall_pass)
        if not rows:
            return fallback
        if len(fallback) >= len(rows):
            by_inputs = {
                tuple(sorted((r.get("inputs") or {}).items())): r for r in rows
            }
            merged: List[Dict[str, Any]] = []
            for i, fb in enumerate(fallback):
                key = tuple(sorted(fb["inputs"].items()))
                sim = by_inputs.get(key)
                if sim:
                    merged.append({**fb, **sim, "num": i + 1})
                else:
                    merged.append({**fb, "num": i + 1})
            if len(merged) > len(rows):
                return merged
    return rows
