"""Clock-domain-crossing (CDC) and reset-domain-crossing (RDC) checks.

This is deliberately a STATIC STRUCTURAL scan, not a formal proof -- the
same category of first-pass check real CDC lint tools (SpyGlass CDC,
Conformal CDC) run before any deeper, netlist-level analysis. It answers
two concrete questions from source text alone:

  1. Which registers does each clock domain drive, and which signals
     cross from one domain into another?
  2. For each crossing, how many register stages does the destination
     domain interpose before using the value in anything other than a
     plain, single-signal capture -- the textbook 2-flop synchronizer
     depth check?

What this deliberately does NOT claim: it cannot see gray-coding,
handshake protocols, or FIFO pointer-comparison logic as "safe" the way
a human reviewer or a real CDC tool with full netlist visibility can --
a synchronizer-depth heuristic on a multi-bit bus is flagged as
categorically riskier than on a single bit for exactly this reason (see
CDCCrossing.note), not silently treated as equivalent. This is a
find-and-report tool: it surfaces every crossing so a human decides
whether it's actually safe, not a tool that certifies safety on its own.

Reset-domain checking follows the same shape: is a signal used as an
asynchronous reset trigger (`posedge`/`negedge` in a sensitivity list,
not the primary clock) a plain top-level input (the expected place for a
design's *own* reset tree to start), or is it derived from combinational
logic within this module -- a real, common RDC bug pattern (a
glitch on that combinational path can assert/deassert reset
asynchronously and unpredictably), reported as such.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .analyzer import Port, PortDirection, RtlModule, _strip_comments
from .always_model import _extract_balanced_block

_ALWAYS_HEADER = re.compile(
    r"always(?:_ff)?\s*@\s*\(([^)]*)\)\s*(begin)?", re.IGNORECASE
)
_LHS_NONBLOCKING = re.compile(r"(\w+)\s*(?:\[[^\]]*\])?\s*<=")
_EDGE_TRIGGER = re.compile(r"\b(posedge|negedge)\s+(\w+)", re.IGNORECASE)


@dataclass
class AlwaysBlock:
    clock_signal: Optional[str]  # first posedge/negedge trigger, if any
    edge_signals: List[str]  # every posedge/negedge-triggered signal (clock + async reset)
    body: str


def _find_always_blocks(clean_body: str) -> List[AlwaysBlock]:
    """Every `always`/`always_ff @(...)` block with its trigger list and
    body -- both the `begin ... end` form and the single-statement form
    (`always @(posedge clk) q <= d;`, no begin/end). The single-statement
    form matters here specifically: it's the common, idiomatic way to
    write exactly one plain synchronizer flop stage, which is the pattern
    this whole module's synchronizer-depth heuristic depends on being able
    to see (confirmed by testing: an earlier version that skipped this
    form entirely missed a textbook 2-flop synchronizer written this way,
    reporting no crossing at all instead of a correctly-synchronized one).
    """
    blocks: List[AlwaysBlock] = []
    for m in _ALWAYS_HEADER.finditer(clean_body):
        sensitivity = m.group(1)
        has_begin = m.group(2) is not None
        edges = [sig for _edge, sig in _EDGE_TRIGGER.findall(sensitivity)]
        clock_signal = edges[0] if edges else None
        if has_begin:
            body, _ = _extract_balanced_block(clean_body, m.end())
        else:
            semi = clean_body.find(";", m.end())
            if semi < 0:
                continue
            body = clean_body[m.end():semi + 1]
        blocks.append(AlwaysBlock(clock_signal=clock_signal, edge_signals=edges, body=body))
    return blocks


def _registers_driven(block_body: str) -> Set[str]:
    return {m.group(1) for m in _LHS_NONBLOCKING.finditer(block_body)}


def _identifiers_referenced(text: str) -> Set[str]:
    return set(re.findall(r"\b[A-Za-z_]\w*\b", text))


@dataclass
class ClockDomain:
    clock_signal: str
    registers: Set[str] = field(default_factory=set)


@dataclass
class CDCCrossing:
    signal: str
    source_domain: str
    dest_domain: str
    width: int
    sync_depth: int  # 0 = used directly (unsynchronized), 1 = one capturing flop, 2+ = textbook depth
    verdict: str  # "UNSYNCHRONIZED" / "WEAK" / "LIKELY_OK"
    note: str


@dataclass
class ResetSignalInfo:
    name: str
    kind: str  # "primary_input" / "combinational" / "registered"
    verdict: str
    note: str


@dataclass
class CDCReport:
    domains: Dict[str, ClockDomain] = field(default_factory=dict)
    crossings: List[CDCCrossing] = field(default_factory=list)
    reset_signals: List[ResetSignalInfo] = field(default_factory=list)

    @property
    def unsynchronized_crossings(self) -> List[CDCCrossing]:
        return [c for c in self.crossings if c.verdict == "UNSYNCHRONIZED"]


def _sync_depth_for_crossing(signal: str, dest_body: str, dest_registers: Set[str]) -> tuple[int, str]:
    """Best-effort synchronizer-chain depth for `signal` inside `dest_body`.

    Walks a chain of "plain capture" registers: `regN <= signal;` (nothing
    else on the RHS) counts as stage 1; if THAT register is itself only
    ever plain-captured into another register (`regN+1 <= regN;`), that's
    stage 2, and so on. The moment `signal` (or the current chain link)
    appears in anything other than a bare `<= identifier;` RHS -- an
    expression, a condition, a concatenation -- the chain stops there:
    that's the depth actually reached before the value is USED, which is
    what matters for metastability resolution, not how many more flops
    exist downstream of that point.
    """
    depth = 0
    current = signal
    seen: Set[str] = set()
    while True:
        # Every assignment whose RHS references `current` at all.
        refs = [
            m for m in re.finditer(rf"(\w+)\s*(?:\[[^\]]*\])?\s*<=\s*([^;]+);", dest_body)
            if re.search(rf"\b{re.escape(current)}\b", m.group(2))
        ]
        if not refs:
            if depth == 0:
                return depth, "the crossing signal itself is never referenced in the destination domain's clocked logic"
            return depth, f"the chain ends after {depth} plain-capture stage(s) with no further consumer -- a normal, expected terminal point, not a problem"
        plain_captures = [
            m for m in refs
            if re.fullmatch(rf"\s*{re.escape(current)}\s*", m.group(2))
        ]
        non_plain = [m for m in refs if m not in plain_captures]
        if non_plain:
            # Used directly in a real expression/condition somewhere --
            # that's the point synchronization needed to have already
            # happened by, regardless of any capture register that also
            # happens to exist elsewhere.
            return depth, f"used directly in a non-capture expression (e.g. `{non_plain[0].group(0).strip()[:60]}`)"
        # Every reference was a plain single-signal capture -- advance
        # the chain through the first such target not already visited
        # (guards against a pathological self-referential loop).
        target = plain_captures[0].group(1)
        if target in seen:
            return depth, "capture chain loops back on itself"
        seen.add(target)
        depth += 1
        current = target
        if depth >= 4:
            return depth, "capture chain traced 4+ stages deep (capped)"


def analyze_cdc(module: RtlModule, rtl_source: str) -> CDCReport:
    """Build a CDCReport: clock domains, every cross-domain signal
    reference, and every async-reset signal's provenance.
    """
    clean = _strip_comments(rtl_source)
    blocks = _find_always_blocks(clean)

    domains: Dict[str, ClockDomain] = {}
    domain_bodies: Dict[str, List[str]] = {}
    for b in blocks:
        if b.clock_signal is None:
            continue
        dom = domains.setdefault(b.clock_signal, ClockDomain(clock_signal=b.clock_signal))
        dom.registers |= _registers_driven(b.body)
        domain_bodies.setdefault(b.clock_signal, []).append(b.body)
    # A synchronizer chain commonly spans more than one `always` block in
    # the same domain (e.g. one block updates a pointer, a separate block
    # captures it into a sync register) -- confirmed a real, not
    # hypothetical, case: a real async FIFO's wr_ptr_sync_commit_reg is
    # captured into wr_ptr_commit_sync_reg in a DIFFERENT always block
    # than the one where the crossing was first detected, and tracing
    # only the first block's text alone underreported a 2-stage chain as
    # 1-stage (WEAK instead of the correct LIKELY_OK). Search the whole
    # domain's combined text, not just one block, for exactly this reason.
    domain_full_text = {clk: "\n".join(bodies) for clk, bodies in domain_bodies.items()}

    port_widths = {p.name: p.width for p in module.ports}

    crossings: List[CDCCrossing] = []
    seen_pairs: Set[tuple[str, str, str]] = set()
    for b in blocks:
        if b.clock_signal is None:
            continue
        dest_clock = b.clock_signal
        dest_registers = domains[dest_clock].registers
        referenced = _identifiers_referenced(b.body)
        for src_clock, src_dom in domains.items():
            if src_clock == dest_clock:
                continue
            crossing_signals = (referenced & src_dom.registers) - dest_registers
            for sig in crossing_signals:
                key = (sig, src_clock, dest_clock)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                depth, why = _sync_depth_for_crossing(sig, domain_full_text[dest_clock], dest_registers)
                width = port_widths.get(sig, 1)
                if depth == 0:
                    verdict = "UNSYNCHRONIZED"
                elif depth == 1:
                    verdict = "WEAK"
                else:
                    verdict = "LIKELY_OK"
                note = f"{depth}-stage capture chain in the '{dest_clock}' domain ({why})."
                if width > 1 and verdict != "UNSYNCHRONIZED":
                    note += (
                        " Multi-bit signal: even an adequate synchronizer depth doesn't make a "
                        "plain register crossing safe on its own -- bits can be sampled mid-"
                        "transition inconsistently across a bus unless it's gray-coded or "
                        "handshaked. Flagging for review regardless of depth."
                    )
                crossings.append(CDCCrossing(
                    signal=sig, source_domain=src_clock, dest_domain=dest_clock,
                    width=width, sync_depth=depth, verdict=verdict, note=note,
                ))

    # Reset-domain check: every distinct edge-triggered signal that is
    # NOT itself used as a clock trigger anywhere (i.e. it's only ever
    # the second+ signal in a sensitivity list) is an async reset
    # candidate.
    clock_signals = set(domains.keys())
    reset_candidates: Set[str] = set()
    for b in blocks:
        for sig in b.edge_signals[1:]:
            if sig not in clock_signals:
                reset_candidates.add(sig)

    input_port_names = {p.name for p in module.ports if p.direction == PortDirection.INPUT}
    all_driven_regs: Set[str] = set()
    for dom in domains.values():
        all_driven_regs |= dom.registers
    # A signal assigned with `assign` (continuous, combinational) rather
    # than driven by any clocked always block, and not a primary input,
    # is combinationally-derived -- the risky RDC case.
    combinational_assigns = {
        m.group(1) for m in re.finditer(r"\bassign\s+(\w+)\s*=", clean)
    }

    reset_signals: List[ResetSignalInfo] = []
    for name in sorted(reset_candidates):
        if name in input_port_names:
            reset_signals.append(ResetSignalInfo(
                name=name, kind="primary_input", verdict="OK",
                note="Top-level input used as an async reset trigger -- the expected shape for "
                     "where a design's own reset tree begins; this module doesn't control "
                     "whether it's properly synchronized upstream.",
            ))
        elif name in all_driven_regs:
            reset_signals.append(ResetSignalInfo(
                name=name, kind="registered", verdict="OK",
                note="Driven by a clocked always block (registered), not raw combinational "
                     "logic -- the expected shape for a locally-generated, synchronized reset.",
            ))
        elif name in combinational_assigns:
            reset_signals.append(ResetSignalInfo(
                name=name, kind="combinational", verdict="RISKY",
                note="Used as an async reset trigger but driven by a continuous `assign` "
                     "(combinational logic), not a register or a primary input -- a glitch on "
                     "that combinational path can assert or deassert reset unpredictably. "
                     "Common, real RDC bug pattern, not a hypothetical one.",
            ))
        else:
            reset_signals.append(ResetSignalInfo(
                name=name, kind="unknown", verdict="UNKNOWN",
                note="Used as an async reset trigger but its origin couldn't be determined from "
                     "this module alone (possibly declared/driven elsewhere, e.g. a generate "
                     "block or a wire this scan didn't trace).",
            ))

    return CDCReport(domains=domains, crossings=crossings, reset_signals=reset_signals)
