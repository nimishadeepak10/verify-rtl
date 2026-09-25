"""Expose DUT internal registers to formal properties -- without ever
editing the DUT source file.

The problem this closes: `formal_props.py`'s wrapper only wires up the
DUT's own ports, so a property can only reference what's visible from
outside (see the `direct_cache.v` stress-test finding in the README --
only 1 of 11 suggested properties was expressible, because the tag/valid
arrays that made the design interesting are internal registers).

Two mechanisms were tried and rejected before this one, both confirmed
empirically (not assumed) to be broken in this project's yosys build
(0.65+67):

  - `bind`: yosys's frontend silently drops standalone `bind` statements
    (the bound module gets logged as unused and removed during hierarchy
    analysis) -- already documented in formal_props.py's own docstring.
  - A hierarchical dot-reference from the wrapper into the DUT instance
    (`dut.internal_reg`), which is otherwise ordinary, legal Verilog: an
    isolated probe showed yosys treats it as an "implicitly declared"
    identifier and a bit-select on it as "out of bounds", silently
    resolving to an UNCONSTRAINED (`undef`) value instead of erroring.
    That produced a real counterexample against a structurally-impossible
    property (a 6-bit register exceeding 63) -- i.e. a FALSE bug report,
    not just a missing feature. Confirmed the same failure mode for both
    a scalar register ("used but has no driver") and an array element.

Ordinary output ports, by contrast, have worked reliably everywhere else
in this project (RVFI, the MESI multi-cache model, every wrapper
`generate_formal_wrapper()` has ever produced). So the mechanism here is
additive RTL instrumentation: parse the DUT, generate a MODIFIED COPY of
its source text with extra output ports wired to the requested internal
registers via plain continuous assignment, and run formal against that
copy. The original RTL file on disk is never touched -- `analyze_rtl()`
is re-run on the modified text to get a fresh, self-consistent RtlModule
whose `.ports` already include the new debug ports, so
`generate_formal_wrapper()` needs no changes at all: it just wires up
ports, same as always.

Array signals get TWO ports instead of one per array element: an input
select index (`__dbgsel_<name>`) and one output (`__dbg_<name>`) reading
`name[__dbgsel_<name>]`. Left as a free input, the solver explores every
index during BMC/PDR on its own -- which is exactly a "for all i" formal
check over the whole array, not a manually unrolled one, and confirmed
working with a genuine (non-vacuous) write-then-read property before
this was wired into the rest of the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from .analyzer import InternalSignal, RtlModule, analyze_rtl

# Default cap on how many internal signals get instrumented when the
# caller doesn't name specific ones. Keeps port count (and therefore
# solver state-space growth) bounded for a design with many registers --
# override by passing explicit `signal_names` to probe exactly what's
# needed.
DEFAULT_MAX_SIGNALS = 24


@dataclass
class ProbedSignal:
    """One instrumented internal signal, with the port names a property
    should reference."""

    source: InternalSignal
    debug_port: str  # e.g. "__dbg_tag_arr" -- the value, width == source.width
    select_port: Optional[str] = None  # e.g. "__dbgsel_tag_arr" -- array index input, or None for scalars


def _port_decl(sig: InternalSignal) -> tuple[list[str], ProbedSignal]:
    """Return (new port declaration lines, ProbedSignal) for one internal signal."""
    debug_name = f"__dbg_{sig.name}"
    rng = f"[{sig.width - 1}:0] " if sig.width > 1 else ""

    if not sig.is_array:
        return ([f"output wire {rng}{debug_name}"], ProbedSignal(source=sig, debug_port=debug_name))

    sel_name = f"__dbgsel_{sig.name}"
    sel_rng = f"[{sig.index_width - 1}:0] " if sig.index_width > 1 else ""
    return (
        [f"input wire {sel_rng}{sel_name}", f"output wire {rng}{debug_name}"],
        ProbedSignal(source=sig, debug_port=debug_name, select_port=sel_name),
    )


def _assign_stmt(probed: ProbedSignal) -> str:
    sig = probed.source
    if not sig.is_array:
        return f"    assign {probed.debug_port} = {sig.name};"
    # array_lo/array_hi are stored exactly as declared (e.g. "[0:3]" gives
    # array_hi=0, array_lo=3 -- literal declaration order, not normalized
    # by magnitude, matching this project's existing Port.msb/lsb
    # convention elsewhere in analyzer.py). The select input is always
    # 0-based, so offset by the true MINIMUM bound, not array_lo by name --
    # confirmed necessary: an earlier version used array_lo directly and
    # produced `tag_arr[__dbgsel_tag_arr + 3]` for a `[0:3]` array, an
    # out-of-range offset for a 2-bit select input (valid 0-3), caught by
    # inspecting the generated RTL before ever running it through a solver.
    base = min(sig.array_lo, sig.array_hi)
    index_expr = probed.select_port if base == 0 else f"({probed.select_port} + {base})"
    return f"    assign {probed.debug_port} = {sig.name}[{index_expr}];"


def _insert_before_module_close_paren(rtl_source: str, module_name: str, extra_ports: List[str]) -> str:
    """Splice `, <extra_ports>` into the module's own port list, just
    before its closing `)`. Reuses analyzer's own paren-matching so this
    handles parameterized headers (`module foo #(...) (...)`) the same
    way analyze_rtl does."""
    from .analyzer import find_hash_paren_close, _find_matching_paren, _skip_ws  # same-package helper reuse
    import re

    m = re.search(rf"\bmodule\s+{re.escape(module_name)}\b", rtl_source)
    if not m:
        raise ValueError(f"Could not find 'module {module_name}' in RTL source")
    idx = _skip_ws(rtl_source, m.end())
    if idx < len(rtl_source) and rtl_source[idx] == "#":
        idx = _skip_ws(rtl_source, find_hash_paren_close(rtl_source, idx) + 1)
    if idx >= len(rtl_source) or rtl_source[idx] != "(":
        raise ValueError(f"Could not find module '{module_name}''s port list")
    close = _find_matching_paren(rtl_source, idx)

    insertion = ",\n    " + ",\n    ".join(extra_ports)
    return rtl_source[:close] + insertion + "\n" + rtl_source[close:]


def _insert_before_endmodule(rtl_source: str, module_name: str, assign_lines: List[str]) -> str:
    import re

    m = re.search(
        rf"\bmodule\s+{re.escape(module_name)}\b.*?\bendmodule\b",
        rtl_source,
        re.DOTALL,
    )
    if not m:
        raise ValueError(f"Could not find 'endmodule' for module {module_name}")
    endmod_idx = m.end() - len("endmodule")
    insertion = "\n" + "\n".join(assign_lines) + "\n\n"
    return rtl_source[:endmod_idx] + insertion + rtl_source[endmod_idx:]


def generate_probed_rtl(
    rtl_source: str,
    module: RtlModule,
    signal_names: Optional[Sequence[str]] = None,
    max_signals: int = DEFAULT_MAX_SIGNALS,
) -> tuple[str, RtlModule, List[ProbedSignal]]:
    """Return (instrumented_rtl_source, instrumented_module, probed_signals).

    `instrumented_rtl_source` is a NEW text -- the original `rtl_source`
    string/file is never modified. `instrumented_module` is the result of
    re-running `analyze_rtl()` on that new text, so its `.ports` already
    include the debug ports and `generate_formal_wrapper()` can be used
    completely unchanged.

    By default, instruments every internal register found (capped at
    `max_signals`, first-declared-first-included). Pass `signal_names` to
    instrument only specific ones (also bypasses the cap).
    """
    available = {s.name: s for s in module.internal_signals}
    if signal_names is not None:
        missing = [n for n in signal_names if n not in available]
        if missing:
            raise ValueError(
                f"Not internal registers of '{module.name}': {missing}. "
                f"Available: {sorted(available)}"
            )
        chosen = [available[n] for n in signal_names]
    else:
        chosen = module.internal_signals[:max_signals]

    if not chosen:
        return rtl_source, module, []

    extra_ports: List[str] = []
    assign_lines: List[str] = []
    probed: List[ProbedSignal] = []
    for sig in chosen:
        decls, p = _port_decl(sig)
        extra_ports.extend(decls)
        assign_lines.append(_assign_stmt(p))
        probed.append(p)

    new_source = _insert_before_module_close_paren(rtl_source, module.name, extra_ports)
    new_source = _insert_before_endmodule(new_source, module.name, assign_lines)

    new_module = analyze_rtl(new_source, top_module=module.name)
    return new_source, new_module, probed
