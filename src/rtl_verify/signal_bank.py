"""Signal-wise information bank: what is known about each signal, gathered
deterministically before any LLM is asked to write an assertion about it.

Adapted from SANGAM (Gupta, Mali, Karfa; IEEE ICLAD 2025), whose first stage
builds a per-signal "information bank" with three LLM agents: a Signal Mapper
(link each signal in the specification to its name in the RTL, and output a
signal only if it appears in BOTH), a Spec Analyzer (definition, functionality,
interconnections and related signals, using nothing outside the document), and
a Waveform Analyzer (interdependence read from specification waveforms).

Two of those jobs have exact answers, so they are done here without a model:

  - Signal mapping is a set intersection between names the specification
    mentions and names the RTL declares. A hallucinated mapping cannot occur
    because nothing is generated.
  - The RTL-side facts (width, direction, the lines that drive the signal, the
    other signals those lines read) are read straight from the source.

What stays for the language model is the part that needs reading
comprehension: turning the collected specification sentences into a claim.
The bank hands it the sentences verbatim, which is also what makes a generated
assertion traceable back to a sentence.

Not adopted: the Waveform Analyzer. It reads diagrams; this project has no
image input, so waveform-derived interdependence is out of scope here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .analyzer import PortDirection, RtlModule

_MAX_RELATED = 8
_MAX_SENTENCES = 6
_MAX_DRIVER_LINES = 6


@dataclass
class SignalInfo:
    name: str                    # name as written in the RTL / specification
    expr_name: str               # name a property must use (a port, or __dbg_<reg>)
    kind: str                    # input | output | register
    width: int
    spec_sentences: List[str] = field(default_factory=list)
    driver_lines: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)

    def prompt_block(self) -> str:
        parts = [f"Signal: {self.name}  (use `{self.expr_name}` in expressions)",
                 f"Kind/width: {self.kind}, {self.width} bit(s)"]
        if self.spec_sentences:
            parts.append("Specification text mentioning it:")
            parts += [f"  - {s}" for s in self.spec_sentences]
        if self.driver_lines:
            parts.append("RTL statements that drive it:")
            parts += [f"  - {s}" for s in self.driver_lines]
        if self.related:
            parts.append("Related signals: " + ", ".join(self.related))
        return "\n".join(parts)


@dataclass
class SignalBank:
    signals: Dict[str, SignalInfo] = field(default_factory=dict)
    unmapped_spec_only: List[str] = field(default_factory=list)   # reserved: names only the spec has
    rtl_only: List[str] = field(default_factory=list)             # in RTL, never in the spec
    used_spec: bool = False


def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [re.sub(r"\s+", " ", p).strip() for p in parts if p.strip()]


def _word_re(name: str) -> "re.Pattern[str]":
    return re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(name)}(?![A-Za-z0-9_])", re.IGNORECASE)


def _driver_lines(rtl_source: str, name: str) -> List[str]:
    pat = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(name)}(?![A-Za-z0-9_])\s*(?:\[[^\]]*\]\s*)?(<=|=)(?!=)")
    assign = re.compile(rf"\bassign\b\s+(?:\[[^\]]*\]\s*)?{re.escape(name)}\b")
    out: List[str] = []
    for line in rtl_source.splitlines():
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        if pat.search(s) or assign.search(s):
            out.append(s)
        if len(out) >= _MAX_DRIVER_LINES:
            break
    return out


def build_signal_bank(module: RtlModule, rtl_source: str, spec_text: str = "") -> SignalBank:
    """`module` must be the PROBED module (internal registers exposed as
    `__dbg_*` ports); `rtl_source` is the user's original text, so driver
    lines quote the real signal names."""
    array_dbg = {p.name[len("__dbgsel_"):] for p in module.inputs if p.name.startswith("__dbgsel_")}
    skip = {module.clock_port, module.reset_port}
    infos: Dict[str, SignalInfo] = {}
    for p in module.ports:
        if p.name.startswith("__dbgsel_"):
            continue
        if p.name.startswith("__dbg_"):
            orig = p.name[len("__dbg_"):]
            if orig in array_dbg:
                continue                     # a memory read through a free index, not one signal
            infos[orig] = SignalInfo(name=orig, expr_name=p.name, kind="register", width=p.width)
        elif p.name not in skip:
            kind = "input" if p.direction == PortDirection.INPUT else "output"
            infos[p.name] = SignalInfo(name=p.name, expr_name=p.name, kind=kind, width=p.width)

    bank = SignalBank(used_spec=bool(spec_text.strip()))
    sentences = _sentences(spec_text) if bank.used_spec else []
    patterns = {n: _word_re(n) for n in infos}
    names = list(infos)
    for n, info in infos.items():
        if sentences:
            info.spec_sentences = [s for s in sentences if patterns[n].search(s)][:_MAX_SENTENCES]
        info.driver_lines = _driver_lines(rtl_source, n)
        rel: List[str] = []
        for text in info.spec_sentences + info.driver_lines:
            for other in names:
                if other != n and other not in rel and patterns[other].search(text):
                    rel.append(other)
        info.related = rel[:_MAX_RELATED]

    # The Signal Mapper rule: with a specification, keep only signals present
    # in both it and the RTL.
    if bank.used_spec:
        bank.rtl_only = [n for n, i in infos.items() if not i.spec_sentences]
        infos = {n: i for n, i in infos.items() if i.spec_sentences}
    bank.signals = infos
    return bank
