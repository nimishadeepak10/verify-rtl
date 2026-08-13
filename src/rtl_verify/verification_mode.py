"""Verification status: synthesis gate + waveform-based behavioral check."""

from __future__ import annotations

from typing import TYPE_CHECKING, Tuple

from .analyzer import RtlModule

if TYPE_CHECKING:
    from .synth_check import SynthCheckResult


def simulation_compile_ok(sim_log: str, backend_used: str = "") -> bool:
    log = sim_log or ""
    if simulation_completed(log):
        return True
    fail_markers = (
        "syntax error",
        "error: malformed",
        "error: [vrfc",
        "error: [xsim",
        "unable to compile",
        "compilation failed",
        "no such file",
        "undefined module",
    )
    lower = log.lower()
    return not any(m in lower for m in fail_markers)


def simulation_completed(sim_log: str) -> bool:
    log = sim_log or ""
    return (
        "RESULT: DONE" in log
        or "SIMULATION COMPLETE" in log
        or "RESULT: PASS" in log
        or "PASS=" in log
    )


def resolve_status(
    mod: RtlModule,
    sim_log: str,
    synth: "SynthCheckResult",
    compile_ok: bool = True,
    sim_completed: bool = True,
) -> Tuple[str, str, str, bool]:
    """
    Returns (verification_mode, verdict, explanation, success_for_api).

    Flow: synthesis check → testbench simulation → waveform review.
    """
    if not synth.skipped and not synth.synthesizable:
        expl = (
            "RTL failed Vivado synthesis — not synthesizable as written. "
            "Fix synthesis errors before behavioral verification."
        )
        return "synth_failed", "not_synthesizable", expl, False

    if not compile_ok or not simulation_compile_ok(sim_log):
        return (
            "compile_failed",
            "error",
            "RTL or testbench failed to compile — simulation did not run.",
            False,
        )

    if not sim_completed or not simulation_completed(sim_log):
        return (
            "sim_failed",
            "error",
            "Simulation did not complete successfully.",
            False,
        )

    synth_line = (
        "Synthesis check passed (Vivado)."
        if not synth.skipped and synth.synthesizable
        else "Synthesis check skipped (Vivado not installed) — simulation only."
    )
    kind = "sequential" if mod.is_sequential else "combinational"
    expl = (
        f"{synth_line}\n"
        f"Directed {kind} testbench ran in the simulator. "
        "Inspect waveforms and simulation log to verify RTL behavior. "
        "No per-test PASS/FAIL — errors appear in the simulation log."
    )
    return "waveform", "pass", expl, True
