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

A real, significant scope boundary, found via testing (not assumed):
this scan only looks at the target module's OWN `always` blocks. It
does not trace into instantiated submodules at all -- so a crossing
synchronized by INSTANTIATING a reusable synchronizer primitive (e.g.
alexforencich/verilog-ethernet's own `sync_signal.v`, a parameterized
N-stage synchronizer meant to be instantiated at every crossing, which
is the idiomatic, professional way real engineers write this, not an
edge case) is completely invisible: the crossing signal only appears as
a port-connection argument in an instantiation statement, never inside
an `always` block body this scanner reads. Confirmed directly: a
two-clock-domain design where domain A's signal is properly
synchronized through an instantiated 2-flop synchronizer submodule
before domain B consumes it reports **zero crossings** -- not
"LIKELY_OK", not "UNSYNCHRONIZED", nothing at all. This is meaningfully
different from every other documented limitation above (which describe
things this scanner sees but can't fully certify) -- here it doesn't
see the crossing exists in the first place. Fixing this properly needs
genuine hierarchical elaboration (parsing instantiation port
connections, resolving each instance's own clock, recursing into its
definition) which this project deliberately hasn't built yet rather
than approximate with an unreliable heuristic; treat a "0 crossings"
result on a design with instantiated synchronizer submodules as "not
analyzed", not "verified safe".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .analyzer import (
    Port,
    PortDirection,
    RtlModule,
    analyze_rtl,
    _extract_module_body,
    _strip_comments,
    strip_ifdef_blocks,
)

# No macro is treated as defined for this scan's purposes -- FORMAL,
# RISCV_FORMAL, DEBUG*, and anything else guarding scaffolding (e.g.
# ZipCPU's `(* gclk *) reg gbl_clk;` proof-only clock abstraction inside
# `ifdef FORMAL`) is excluded, since none of it is genuinely
# always-synthesized hardware, regardless of what a given formal *run*
# happens to define (unlike `analyze_rtl()`'s own `strip_ifdef_blocks`
# call, which matches SymbiYosys's actual `-formal` convention instead).
_VERIFICATION_ONLY_MACROS: Set[str] = set()
from .always_model import _extract_balanced_block

_ALWAYS_HEADER = re.compile(
    r"always(?:_ff)?\s*@\s*\(([^)]*)\)\s*(begin)?", re.IGNORECASE
)
_CONCAT_LHS_NONBLOCKING = re.compile(r"\{([^{}]+)\}\s*<=")
_CONCAT_SHIFT_ASSIGN = re.compile(r"\{([^{}]+)\}\s*<=\s*\{([^{}]+)\}\s*;")
_EDGE_TRIGGER = re.compile(r"\b(posedge|negedge)\s+(\w+)", re.IGNORECASE)


@dataclass
class AlwaysBlock:
    clock_signal: Optional[str]  # first posedge/negedge trigger, if any
    edge_signals: List[str]  # every posedge/negedge-triggered signal (clock + async reset)
    body: str


def _consume_stmt_end(text: str, i: int) -> int:
    """Index just past the end of the single Verilog statement starting at
    `i` (leading whitespace skipped): a `begin ... end` block, an
    `if (...) stmt [else stmt]` chain (each arm itself a nested statement,
    recursively), or a plain `...;`-terminated statement.

    Needed because an `always` block with no *top-level* `begin`/`end` can
    still be a compound `if/else` whose arms individually have no
    `begin`/`end` either -- naively stopping at the first `;` truncates
    the `else` arm entirely. Confirmed a real, not hypothetical, miss:
    a real async-FIFO gray-pointer synchronizer capture written exactly
    this way (`if (!rst) x <= 0; else x <= y;`) had its `else` branch --
    the actual capture -- silently dropped before this was added,
    reporting a real, correctly-synchronized crossing as never
    referenced at all.
    """
    n = len(text)
    j = i
    while j < n and text[j].isspace():
        j += 1

    def _word_at(pos: int, word: str) -> bool:
        end = pos + len(word)
        if text[pos:end] != word:
            return False
        return end == n or not (text[end].isalnum() or text[end] == "_")

    if _word_at(j, "begin"):
        _, end = _extract_balanced_block(text, j + 5)
        return end
    if _word_at(j, "if"):
        paren = text.find("(", j)
        if paren < 0:
            semi = text.find(";", j)
            return (semi + 1) if semi >= 0 else n
        depth = 0
        k = paren
        while k < n:
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
        then_end = _consume_stmt_end(text, k)
        m = then_end
        while m < n and text[m].isspace():
            m += 1
        if _word_at(m, "else"):
            return _consume_stmt_end(text, m + 4)
        return then_end
    semi = text.find(";", j)
    return (semi + 1) if semi >= 0 else n


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
            end = _consume_stmt_end(clean_body, m.end())
            body = clean_body[m.end():end]
        blocks.append(AlwaysBlock(clock_signal=clock_signal, edge_signals=edges, body=body))
    return blocks


def _lhs_identifier_before(text: str, pos: int) -> Optional[str]:
    """Walk backward from `pos` (the start of a `<=`) over a plain LHS --
    an identifier optionally followed by one or more `[...]` index
    groups, each possibly containing its own nested brackets (e.g. a
    memory-array write like
    `mem[wr_addr[SEG_ADDR_WIDTH*n +: INT_ADDR_WIDTH]][i*8 +: 8] <= data;`,
    a real pattern from a genuine async dual-port RAM). Returns the base
    identifier, or None if the LHS isn't a plain (optionally indexed)
    identifier (e.g. a concatenation target, handled separately).

    `_LHS_NONBLOCKING`'s single-bracket, non-nested regex couldn't see
    this shape at all -- confirmed a real, serious miss on
    alexforencich/verilog-pcie's dma_psdpram_async.v: the memory array
    driven in the write-clock domain was never recognized as a register
    at all, so the genuine write-clock -> read-clock crossing through it
    was silently invisible (zero crossings reported), not merely
    misclassified.
    """
    j = pos
    while j > 0 and text[j - 1].isspace():
        j -= 1
    while j > 0 and text[j - 1] == "]":
        depth = 0
        k = j - 1
        while k >= 0:
            if text[k] == "]":
                depth += 1
            elif text[k] == "[":
                depth -= 1
                if depth == 0:
                    break
            k -= 1
        if k < 0:
            return None
        j = k
    end = j
    while j > 0 and (text[j - 1].isalnum() or text[j - 1] == "_"):
        j -= 1
    if j == end:
        return None
    return text[j:end]


def _registers_driven(block_body: str) -> Set[str]:
    regs: Set[str] = set()
    for m in re.finditer(r"<=", block_body):
        ident = _lhs_identifier_before(block_body, m.start())
        if ident:
            regs.add(ident)
    for m in _CONCAT_LHS_NONBLOCKING.finditer(block_body):
        regs |= {t.strip() for t in m.group(1).split(",") if re.fullmatch(r"\w+", t.strip())}
    return regs


def _concat_shift_advance(dest_body: str, current: str) -> Optional[tuple[str, int]]:
    """Detect the common shift-register-via-concatenation synchronizer
    idiom: `{ stageN, ..., stage1 } <= { stageN-1, ..., stage1, source };`
    -- every clock, the whole bit vector shifts one register-width to the
    left and `source` is captured in at the tail. This is a real, common
    way to write a multi-flop synchronizer (seen in ZipCPU-style RTL,
    e.g. gray-code pointer synchronizers packing several cross-domain
    flops into one concatenated shift register) that a plain single-
    identifier `reg <= reg;` scan can't see at all -- confirmed missed
    entirely before this was added, silently reporting a real,
    correctly-synchronized crossing as unsynchronized.

    Returns (new_current, stages_advanced) if `current` is the RHS's
    last (innermost/newest) term and the rest of the RHS matches the
    LHS shifted by one position, else None.
    """
    for m in _CONCAT_SHIFT_ASSIGN.finditer(dest_body):
        lhs_terms = [t.strip() for t in m.group(1).split(",")]
        rhs_terms = [t.strip() for t in m.group(2).split(",")]
        if len(lhs_terms) < 2 or len(lhs_terms) != len(rhs_terms):
            continue
        if rhs_terms[-1] == current and lhs_terms[1:] == rhs_terms[:-1]:
            return lhs_terms[0], len(lhs_terms)
    return None


def _identifiers_referenced(text: str) -> Set[str]:
    return set(re.findall(r"\b[A-Za-z_]\w*\b", text))


def _find_matching_close(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Index of the `close_ch` matching the `open_ch` at `open_idx`, or -1."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


@dataclass
class Instantiation:
    module_name: str
    instance_name: str
    connections: Dict[str, str]  # port name -> connected signal text (raw, stripped)


_IDENT_ONLY = re.compile(r"\w+")
_SIMPLE_ASSIGN_ALIAS = re.compile(r"\bassign\s+(\w+)\s*=\s*(\w+)\s*;")


def _resolve_clock_alias(clean_body: str, signal: str, domains: Dict[str, ClockDomain]) -> str:
    """If `signal` isn't itself a known domain but is a simple continuous-
    assignment alias of one (`assign signal = other;`), resolve to the
    aliased domain's name instead of treating the alias as a genuinely
    different clock domain.

    A real, common wrapper idiom -- confirmed directly on
    YosysHQ/picorv32's own `picorv32_wb`, which does
    `assign clk = wb_clk_i;` before instantiating the CPU core with
    `.clk(clk)`. Without this resolution, the hierarchical tracer would
    treat `clk` as a different domain from `wb_clk_i` even though
    they're the exact same physical clock net under two names,
    fabricating a fake crossing between a signal and itself.
    """
    seen = {signal}
    current = signal
    aliases: Optional[Dict[str, str]] = None
    while current not in domains:
        if aliases is None:
            aliases = dict(_SIMPLE_ASSIGN_ALIAS.findall(clean_body))
        nxt = aliases.get(current)
        if nxt is None or nxt in seen:
            break
        seen.add(nxt)
        current = nxt
    return current


def _split_concat_identifiers(conn_sig: str) -> Optional[List[str]]:
    """If `conn_sig` is a bare identifier, or a `{a, b, c}` concatenation
    of bare identifiers -- a real, common way to bundle several signals
    onto one port at an instantiation (e.g. `.in({uart_rxd, uart_cts})`,
    from a real board-level integration file) -- return the constituent
    identifier(s); else None (an arbitrary expression this scope
    deliberately doesn't resolve, rather than guessing at it)."""
    if re.fullmatch(r"\w+", conn_sig):
        return [conn_sig]
    if conn_sig.startswith("{") and conn_sig.endswith("}"):
        terms = [t.strip() for t in conn_sig[1:-1].split(",")]
        if terms and all(re.fullmatch(r"\w+", t) for t in terms):
            return terms
    return None


def _find_instantiations(clean_body: str, known_modules: Set[str]) -> List[Instantiation]:
    """Every `MODULE_NAME [#(...)] instance_name (.port(sig), ...);`
    instantiation of a module DEFINED IN THIS SAME SOURCE TEXT, found in
    `clean_body`. Restricting to known module names (rather than a
    generic "identifier identifier (" pattern) avoids false positives
    from other statement shapes that superficially resemble one.

    Only NAMED port connections (`.port(signal)`) whose connected signal
    is a bare identifier are resolved here -- positional connections and
    connections that are themselves expressions (not a simple wire/reg
    name) are silently skipped for hierarchical tracing (see
    `_hierarchical_crossings`), a deliberate, narrow scope limit rather
    than an unreliable heuristic over arbitrary expressions.
    """
    if not known_modules:
        return []
    names_pattern = "|".join(re.escape(n) for n in sorted(known_modules, key=len, reverse=True))
    header_re = re.compile(rf"^[ \t]*({names_pattern})\b", re.MULTILINE)
    results: List[Instantiation] = []
    n = len(clean_body)
    for m in header_re.finditer(clean_body):
        module_name = m.group(1)
        i = m.end()
        while i < n and clean_body[i].isspace():
            i += 1
        if i < n and clean_body[i] == "#":
            close = _find_matching_close(clean_body, i + 1, "(", ")")
            if close < 0:
                continue
            i = close + 1
            while i < n and clean_body[i].isspace():
                i += 1
        inst_m = _IDENT_ONLY.match(clean_body, i)
        if not inst_m:
            continue
        instance_name = inst_m.group(0)
        i = inst_m.end()
        while i < n and clean_body[i].isspace():
            i += 1
        if i >= n or clean_body[i] != "(":
            continue
        close = _find_matching_close(clean_body, i, "(", ")")
        if close < 0:
            continue
        port_list_text = clean_body[i + 1:close]
        j = close + 1
        while j < n and clean_body[j].isspace():
            j += 1
        if j >= n or clean_body[j] != ";":
            continue  # not actually an instantiation statement -- skip
        connections: Dict[str, str] = {}
        for pm in re.finditer(r"\.(\w+)\s*\(", port_list_text):
            popen = pm.end() - 1
            pclose = _find_matching_close(port_list_text, popen, "(", ")")
            if pclose < 0:
                continue
            connections[pm.group(1)] = port_list_text[popen + 1:pclose].strip()
        results.append(Instantiation(module_name=module_name, instance_name=instance_name, connections=connections))
    return results


def _compute_domains(clean_body: str) -> tuple[Dict[str, ClockDomain], Dict[str, str], List[AlwaysBlock]]:
    """Group a module's `always` blocks into clock domains: which
    registers each domain drives, and (for capture-depth tracing) each
    domain's combined body text across every block that shares its
    clock -- factored out so both a module's own crossing detection and
    the hierarchical tracer's per-callee analysis (see
    `_hierarchical_crossings`) use the exact same logic.
    """
    blocks = _find_always_blocks(clean_body)
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
    return domains, domain_full_text, blocks


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
        concat_hit = _concat_shift_advance(dest_body, current)
        if concat_hit is not None:
            target, stages = concat_hit
            if target in seen:
                return depth, "capture chain loops back on itself"
            seen.add(target)
            depth += stages
            current = target
            if depth >= 4:
                return depth, "capture chain traced 4+ stages deep (capped)"
            continue
        # Every assignment whose RHS references `current` at all. Finds
        # the LHS via the same bracket-aware backward matcher as
        # _registers_driven -- a nested-bracket LHS (a memory-array
        # write) must be recognized as a capture target here too, not
        # just when first cataloguing a domain's registers.
        refs: List[tuple[str, str, str]] = []  # (lhs_ident, rhs, display_text)
        for assign_m in re.finditer(r"<=", dest_body):
            lhs_ident = _lhs_identifier_before(dest_body, assign_m.start())
            if lhs_ident is None:
                continue
            semi = dest_body.find(";", assign_m.end())
            if semi < 0:
                continue
            rhs = dest_body[assign_m.end():semi]
            if re.search(rf"\b{re.escape(current)}\b", rhs):
                lhs_start = assign_m.start()
                while lhs_start > 0 and dest_body[lhs_start - 1] not in "\n;":
                    lhs_start -= 1
                refs.append((lhs_ident, rhs, dest_body[lhs_start:semi + 1].strip()))
        if not refs:
            if depth == 0:
                return depth, "the crossing signal itself is never referenced in the destination domain's clocked logic"
            return depth, f"the chain ends after {depth} plain-capture stage(s) with no further consumer -- a normal, expected terminal point, not a problem"
        plain_captures = [
            ref for ref in refs
            if re.fullmatch(rf"\s*{re.escape(current)}\s*", ref[1])
        ]
        non_plain = [ref for ref in refs if ref not in plain_captures]
        if non_plain:
            # Used directly in a real expression/condition somewhere --
            # that's the point synchronization needed to have already
            # happened by, regardless of any capture register that also
            # happens to exist elsewhere.
            example = non_plain[0][2][:60]
            if re.search(rf"\b{re.escape(current)}\s*\[", non_plain[0][1]):
                # `current[...]` on the RHS -- an array/memory read, not a
                # flip-flop reference. Real, not hypothetical: a genuine
                # async dual-port RAM (alexforencich/verilog-pcie's
                # dma_psdpram_async.v) crosses domains exactly this way,
                # through a shared `mem` array written in one clock and
                # read in another -- a fundamentally different mechanism
                # from a flop-based synchronizer (the memory primitive
                # itself has to be metastability-safe for this access
                # pattern), so the generic "add a sync stage" framing
                # would be actively misleading here.
                return depth, (
                    f"read directly from an array/memory indexed by a value from the "
                    f"other domain (e.g. `{example}`) -- this is a memory-based crossing, "
                    "not a flip-flop one: adding a synchronizer register doesn't apply the "
                    "same way here. Verify the underlying memory is a genuine dual-port "
                    "primitive safe for this access pattern, not a plain register array."
                )
            return depth, f"used directly in a non-capture expression (e.g. `{example}`)"
        # Every reference was a plain single-signal capture -- advance
        # the chain through the first such target not already visited
        # (guards against a pathological self-referential loop).
        target = plain_captures[0][0]
        if target in seen:
            return depth, "capture chain loops back on itself"
        seen.add(target)
        depth += 1
        current = target
        if depth >= 4:
            return depth, "capture chain traced 4+ stages deep (capped)"


def _hierarchical_crossings(
    module: RtlModule,
    rtl_source: str,
    clean_body: str,
    domains: Dict[str, ClockDomain],
    seen_pairs: Set[tuple[str, str, str]],
) -> List[CDCCrossing]:
    """Trace crossings synchronized by INSTANTIATING a reusable
    synchronizer submodule, rather than hand-inlining flip-flops in the
    consuming module -- the idiomatic, professional way real engineers
    write this (see this module's own docstring for the real-world case,
    alexforencich/verilog-ethernet's `sync_signal.v`, that motivated
    building this). Before this function existed, such a crossing was
    completely invisible: the crossing signal only ever appears as a
    port-connection argument in an instantiation statement, never inside
    an `always` block body the rest of this scanner reads.

    For each directly-instantiated module DEFINED IN THIS SAME SOURCE
    TEXT (one level deep -- see the scope note below), this:
      1. Resolves which caller-side signal drives the instance's own
         clock port (`.clk_port_name(some_signal)`); that signal becomes
         the instance's clock-domain name, reusing an existing domain of
         the same name if the caller already has one.
      2. Attributes every instance OUTPUT port's connected signal to
         that domain, so ordinary same-module crossing detection (in
         `analyze_cdc`) picks up any FURTHER downstream use of that
         signal in a different domain for free, with no new logic.
      3. For every instance INPUT port whose connected signal belongs to
         one of the caller's OTHER domains, reports a crossing -- with
         the capture depth traced INSIDE the callee's own body (reusing
         `_sync_depth_for_crossing` exactly as intra-module crossings
         do), so a genuinely well-synchronized instantiated primitive
         still reports `LIKELY_OK`, not a blanket "can't tell."

    Scope, deliberately: only ONE level of instantiation is traced (the
    target module's own direct instantiations, not its instantiations'
    instantiations) -- full recursive elaboration to arbitrary depth is
    a substantially larger feature; analyze each intermediate module
    directly (as this project's own test suite already does for every
    real design) to see crossings nested deeper than that. Only NAMED
    port connections with a bare-identifier signal are resolved (see
    `_find_instantiations`); a submodule not defined in this same source
    text, or itself a documented-unsupported multi-clock module with no
    single identifiable clock port, is skipped gracefully, not guessed at.
    """
    defined_modules = set(re.findall(r"\bmodule\s+(\w+)\b", rtl_source)) - {module.name}
    instantiations = _find_instantiations(clean_body, defined_modules)
    crossings: List[CDCCrossing] = []

    for inst in instantiations:
        try:
            callee_mod = analyze_rtl(rtl_source, top_module=inst.module_name)
        except Exception:
            continue  # callee not resolvable from this source text -- skip gracefully
        if not callee_mod.is_sequential or not callee_mod.clock_port:
            continue  # combinational, or itself an unsupported multi-clock module -- skip

        clk_conn = inst.connections.get(callee_mod.clock_port)
        if not clk_conn or not re.fullmatch(r"\w+", clk_conn):
            continue  # can't resolve a simple clock connection -- documented scope limit

        instance_domain = _resolve_clock_alias(clean_body, clk_conn, domains)
        callee_clean = strip_ifdef_blocks(
            _extract_module_body(_strip_comments(rtl_source), callee_mod.name),
            _VERIFICATION_ONLY_MACROS,
        )
        callee_domains, callee_domain_text, _ = _compute_domains(callee_clean)
        callee_dom = callee_domains.get(callee_mod.clock_port)
        callee_registers = callee_dom.registers if callee_dom else set()
        callee_body_text = callee_domain_text.get(callee_mod.clock_port, "")
        callee_port_dirs = {p.name: p.direction for p in callee_mod.ports}

        dom = domains.setdefault(instance_domain, ClockDomain(clock_signal=instance_domain))
        for port_name, conn_sig in inst.connections.items():
            if port_name == callee_mod.clock_port:
                continue
            idents = _split_concat_identifiers(conn_sig)
            if idents is None:
                continue
            if callee_port_dirs.get(port_name) == PortDirection.OUTPUT:
                dom.registers.update(idents)

        for port_name, conn_sig in inst.connections.items():
            if port_name == callee_mod.clock_port:
                continue
            if callee_port_dirs.get(port_name) != PortDirection.INPUT:
                continue
            idents = _split_concat_identifiers(conn_sig)
            if idents is None:
                continue
            src_domain_name = next((
                src_name for src_name, src_dom in domains.items()
                if src_name != instance_domain and any(ident in src_dom.registers for ident in idents)
            ), None)
            if src_domain_name is None:
                continue
            key = (f"{inst.instance_name}.{port_name}", src_domain_name, instance_domain)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            depth, why = _sync_depth_for_crossing(port_name, callee_body_text, callee_registers)
            if depth == 0:
                verdict = "UNSYNCHRONIZED"
            elif depth == 1:
                verdict = "WEAK"
            else:
                verdict = "LIKELY_OK"
            note = (
                f"Crosses via instantiated submodule '{inst.instance_name}' "
                f"({inst.module_name}), port '{port_name}': {depth}-stage capture chain "
                f"inside that submodule's own '{callee_mod.clock_port}' domain ({why})."
            )
            crossings.append(CDCCrossing(
                signal=f"{inst.instance_name}.{port_name}",
                source_domain=src_domain_name, dest_domain=instance_domain,
                width=1, sync_depth=depth, verdict=verdict, note=note,
            ))
    return crossings


def analyze_cdc(module: RtlModule, rtl_source: str) -> CDCReport:
    """Build a CDCReport: clock domains, every cross-domain signal
    reference (both hand-inlined and via an instantiated synchronizer
    submodule one level deep -- see `_hierarchical_crossings`), and
    every async-reset signal's provenance.
    """
    # Scope the scan to this module's own body -- rtl_source may contain
    # other modules entirely (a real, not hypothetical, risk: confirmed on
    # picorv32.v, whose file defines 8 modules; scanning the raw file text
    # misattributed a completely different module's `wb_clk_i`-clocked
    # always blocks to the actual `picorv32` core, fabricating a second
    # clock domain and several crossings that don't exist in that module).
    module_body = _extract_module_body(_strip_comments(rtl_source), module.name)
    clean = strip_ifdef_blocks(module_body, _VERIFICATION_ONLY_MACROS)
    domains, domain_full_text, blocks = _compute_domains(clean)

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

    crossings.extend(_hierarchical_crossings(module, rtl_source, clean, domains, seen_pairs))

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
