"""Derive verification mode and user-facing verdict from RTL + simulation outcome."""

from __future__ import annotations

from typing import Optional, Tuple

from .analyzer import RtlModule
from .combinational_model import can_self_check


def derive_verification_mode(
    mod: RtlModule,
    rtl_source: str,
    sim_log: str,
    compile_ok: bool = True,
) -> str:
    """One of: self_checking | monitor_only | compile_failed | sim_failed."""
    log_lower = (sim_log or "").lower()
    if not compile_ok or "error:" in log_lower and "iverilog" in log_lower:
        if any(
            x in log_lower
            for x in (
                "syntax error",
                "error:",
                "undefined",
                "unable to elaborate",
                "compilation failed",
            )
        ) and "result: pass" not in log_lower:
            return "compile_failed"
    if any(x in log_lower for x in ("segmentation fault", "simulation aborted", "timeout")):
        return "sim_failed"
    if mod.is_sequential:
        return "monitor_only"
    if rtl_source and can_self_check(rtl_source, mod):
        return "self_checking"
    return "monitor_only"


def derive_verdict(
    verification_mode: str,
    sim_log: str,
    sim_completed: bool = True,
) -> str:
    """One of: pass | fail | unverified | error"""
    log = sim_log or ""
    if verification_mode == "compile_failed":
        return "error"
    if verification_mode == "sim_failed" or not sim_completed:
        return "error"
    sim_ok = "RESULT: PASS" in log or "RESULT: FAIL" in log or "PASS=" in log
    if verification_mode == "self_checking":
        if "RESULT: FAIL" in log:
            return "fail"
        if "RESULT: PASS" in log:
            return "pass"
        return "error" if not sim_ok else "fail"
    if verification_mode == "monitor_only":
        if not sim_ok and any(x in log.lower() for x in ("error", "failed")):
            return "error"
        return "unverified"
    return "error"


def verification_mode_explanation(
    verification_mode: str,
    mod: RtlModule,
    rtl_source: str = "",
) -> str:
    if verification_mode == "self_checking":
        if mod.inferred_op == "case_dispatch":
            return (
                "Self-checking enabled: golden model derived from RTL case/ternary structure."
            )
        return "Self-checking enabled: golden model derived from RTL structure."
    if verification_mode == "compile_failed":
        return "RTL or testbench failed to compile — simulation did not run."
    if verification_mode == "sim_failed":
        return "Simulation crashed or did not complete."
    if mod.is_sequential:
        return (
            "Sequential design — monitor-only stimulus (no combinational golden model)."
        )
    if mod.unsupported_constructs:
        parts = [
            "Could not derive a golden model. Unsupported constructs:",
            *[f"  • {m}" for m in mod.unsupported_constructs],
            "Outputs were logged but not compared to expected values.",
        ]
        return "\n".join(parts)
    if mod.inferred_op == "unverifiable":
        return (
            "Could not derive a golden model from the RTL — outputs were logged but not "
            "compared to expected values. See Full report for unsupported constructs."
        )
    return (
        "Could not derive a golden model from the RTL — outputs were logged but not "
        "compared to expected values. PASS in the log means the simulator ran without "
        "errors, not that outputs are correct."
    )


def resolve_status(
    mod: RtlModule,
    rtl_source: str,
    sim_log: str,
    compile_ok: bool = True,
    sim_completed: bool = True,
) -> Tuple[str, str, str, bool]:
    """
    Returns (verification_mode, verdict, explanation, success_for_api).
    success_for_api is True only for verdict pass (not unverified).
    """
    mode = derive_verification_mode(mod, rtl_source, sim_log, compile_ok=compile_ok)
    verdict = derive_verdict(mode, sim_log, sim_completed=sim_completed)
    explanation = verification_mode_explanation(mode, mod, rtl_source)
    success = verdict == "pass"
    return mode, verdict, explanation, success
