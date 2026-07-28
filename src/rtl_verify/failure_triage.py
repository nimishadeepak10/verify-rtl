"""Phase 5: failure triage / waveform chat.

No new data model — this packages what a completed /api/verify run
already produces (the RTL, report.txt, and the VCD-derived waveform
JSON) as chat context, and answers questions about that one run,
citing the specific RTL line and/or the specific signal/timestamp in
the waveform rather than answering in the abstract.

Scoped to a single run's work_dir — no cross-run history.
"""

from __future__ import annotations

from pathlib import Path

from . import llm_client
from .coverage import build_env_timeline_from_vcd_signals
from .waveform import load_module_info, vcd_to_json

_RTL_EXTENSIONS = (".sv", ".v")


def _find_rtl_source(work_dir: Path) -> str:
    for ext in _RTL_EXTENSIONS:
        path = work_dir / f"dut{ext}"
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _find_report_text(work_dir: Path) -> str:
    path = work_dir / "report.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _waveform_summary(work_dir: Path, max_signals: int = 40, max_rows: int = 200) -> str:
    """A compact, LLM-readable rendering of the VCD-derived signals, as
    one row per timestamp with every signal's value at that time — not a
    per-signal comma-joined transition list.

    Found live (asking "what are a/b/sum at t=20000ns") that a per-signal
    list of binary-string transitions is a real place for the model to
    misread which timestamp a value belongs to — it answered confidently
    with a wrong value for one signal at one timestamp even though the
    correct value was right there in the context, then (correctly, but
    only after being wrong first) flagged its own "discrepancy" as
    unexplained. Pivoting to one row per timestamp, with plain decimal
    values (via the same env-timeline this project's coverage engine
    already uses) rather than binary strings the model would have to
    convert itself, removes that specific failure mode rather than just
    asking the model to be more careful.
    """
    vcd_path = work_dir / "sim.vcd"
    if not vcd_path.is_file():
        return "(no waveform available for this run)"
    module_info = load_module_info(work_dir)
    wave = vcd_to_json(vcd_path, module_info=module_info)
    signals = wave.get("signals") if isinstance(wave, dict) else None
    if not isinstance(signals, list) or not signals:
        return "(waveform parsed but contained no signals)"

    shown_signals = signals[:max_signals]
    names = [s.get("name", "?") for s in shown_signals]
    timeline = build_env_timeline_from_vcd_signals(shown_signals)
    times = sorted(timeline.keys())[:max_rows]

    header = "time(ns) | " + " | ".join(names)
    lines = [
        f"timescale: {wave.get('timescale', '?')}, end_time: {wave.get('end_time', '?')}",
        header,
    ]
    for t in times:
        row = timeline[t]
        vals = [("?" if row.get(n) is None else str(row.get(n))) for n in names]
        lines.append(f"{t} | " + " | ".join(vals))
    if len(timeline) > max_rows:
        lines.append(f"... (+{len(timeline) - max_rows} more timestamps, not shown)")
    if len(signals) > max_signals:
        lines.append(f"... (+{len(signals) - max_signals} more signals, not shown)")
    return "\n".join(lines)


def load_run_context(work_dir: Path) -> dict:
    return {
        "rtl_source": _find_rtl_source(work_dir),
        "report_text": _find_report_text(work_dir),
        "waveform_summary": _waveform_summary(work_dir),
    }


_SYSTEM = """You are helping a verification engineer understand the results of one specific \
simulation/formal run. Answer ONLY from the RTL, report, and waveform data given below — never \
invent signal values, line numbers, or behavior that isn't actually in this context.

When your answer depends on something the RTL does, cite the specific line number. When it \
depends on signal behavior, cite the specific signal name and timestamp(s) from the waveform \
data — not a vague description like "later in the simulation."

If the given context genuinely doesn't contain enough information to answer (e.g. the waveform \
was truncated, or the question asks about something this run didn't exercise), say so plainly \
instead of guessing — an honest "I can't tell from this run" is far more useful than a made-up \
answer.
"""


def build_user_prompt(context: dict, question: str, history: list[dict]) -> str:
    parts = [
        "RTL source (line numbers added):",
        "```",
        "\n".join(f"{i + 1}: {line}" for i, line in enumerate((context.get("rtl_source") or "").splitlines())),
        "```",
        "",
        "Simulation/formal report:",
        "```",
        (context.get("report_text") or "")[-6000:],
        "```",
        "",
        "Waveform signal transitions:",
        "```",
        context.get("waveform_summary") or "",
        "```",
    ]
    if history:
        parts.append("")
        parts.append("Prior conversation about this same run:")
        for turn in history[-6:]:
            q = str(turn.get("question", "")).strip()
            a = str(turn.get("answer", "")).strip()
            if q:
                parts.append(f"Q: {q}")
            if a:
                parts.append(f"A: {a}")
    parts.append("")
    parts.append(f"Question: {question.strip()}")
    return "\n".join(parts)


def answer_question(work_dir: Path, question: str, history: list[dict] | None = None) -> str:
    context = load_run_context(work_dir)
    user = build_user_prompt(context, question, history or [])
    return llm_client.complete(_SYSTEM, user, max_tokens=1500)
