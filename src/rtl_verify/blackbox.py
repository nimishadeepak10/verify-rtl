"""Design-size reduction via black-boxing: replace a chosen submodule with
a stub of the same name and port list, whose outputs are left genuinely
undriven, so the solver treats them as free/unconstrained rather than
reasoning about that submodule's actual internal logic at all.

Why this matters: a real chip has many genuinely-used blocks, and a
property about ONE of them (a bus arbiter's grant logic, say) can still
be forced to depend -- through a shared status/busy signal, not through
disconnected dead code -- on facts about a much larger, unrelated block's
internal computation (a DSP pipeline, a large multiplier). A solver's own
automatic cone-of-influence reduction already discards *disconnected*
logic for free (confirmed empirically: two genuinely unrelated blocks in
one design showed no speed difference with or without black-boxing,
because the solver never needed to look at the unrelated block at all).
It can NOT discard a block that genuinely feeds into the property's logic
this way, even when the property's truth doesn't actually depend on the
specific values that block computes -- confirmed empirically here too: a
24-stage 32x32 multiply-accumulate pipeline gating a bus arbiter's grant
signal through nothing more than "is the top half of the accumulator
nonzero" made an otherwise-trivial one-hot-grant proof measurably
slower (~9x on the design this was validated against), purely because
the accumulator's real arithmetic entered the property's cone of
influence. Black-boxing that pipeline recovers the fast proof (see
scripts/test_blackbox_reduction.py for the actual measured numbers) --
correctly, since the property never depended on what value the pipeline
actually produces, only that busy_block/mac_busy exists as *some* signal.

Two other techniques were tried first and rejected, both confirmed by
direct testing, not assumed:

  - `(* blackbox *)` on the submodule: this is a SYNTHESIS-time construct.
    sby's default `[script]` (`read -formal ...; prep -top ...`) runs a
    `check` pass that rejects outright any non-top blackbox module
    present in the design ("... is a blackbox/whitebox module") -- there
    is no flag in the default flow to permit this and continue.
  - A stub with a completely EMPTY body (no cells at all): yosys treats
    any module with no internal cells as an IMPLICIT blackbox, hitting
    the identical `check`-pass rejection as the explicit attribute above.

The technique that actually works: keep the stub's body non-empty (a
single, output-unrelated dummy wire assignment is enough) so yosys does
NOT treat it as a blackbox at all, while genuinely leaving each output
port undriven. Confirmed doing what it's supposed to with a dedicated
safety probe: an undriven-output stub's assertion claiming its output
"is always 0" came back FALSIFIED by a real PDR counterexample -- the
solver genuinely explored the output taking value 1, not a fixed or
stuck value. (A secondary, narrower issue found along the way: yosys-
smtbmc's own VCD trace-generation step can crash trying to name-resolve
a fully undriven signal when rendering a FAILING counterexample's trace
-- the PASS/FAIL verdict itself was still correct; only the human-
readable trace for that specific case didn't render. Not something this
module can fix -- it's third-party witness-conversion code -- and not
relevant to this feature's actual use case, which targets PASS/PROVEN
results on properties that don't directly reference the blackboxed
signal in a failing way.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Set

from .analyzer import (
    PortDirection,
    RtlModule,
    find_hash_paren_close,
    _extract_module_body,
    _skip_ws,
    _strip_comments,
    analyze_rtl,
)
from .cdc_check import _find_instantiations


def _find_module_span(rtl_source: str, module_name: str) -> tuple[int, int]:
    """Return (start, end) indices spanning `module <name> ... endmodule`
    (inclusive of both keywords) for the named module in rtl_source."""
    m = re.search(rf"\bmodule\s+{re.escape(module_name)}\b", rtl_source)
    if not m:
        raise ValueError(f"Module '{module_name}' not found in the given RTL source")
    end_m = re.search(r"\bendmodule\b", rtl_source[m.start():])
    if not end_m:
        raise ValueError(f"Module '{module_name}' has no matching 'endmodule'")
    return m.start(), m.start() + end_m.end()


def _extract_param_header(original_module_text: str, module_name: str) -> str:
    """Return the raw `#( ... )` parameter port list text (including the
    `#(` and `)`), or "" if the module has none. Preserved verbatim in the
    stub -- a parameterized instantiation (`foo #(.WIDTH(8)) u_foo (...)`)
    needs the stub to declare the same parameters, or elaboration fails
    with "Can't find object for defparam" (confirmed by hitting exactly
    this error before adding this step: a real instantiation in this
    project's own synthetic test design uses `#(.STAGES(24))`)."""
    m = re.search(rf"\bmodule\s+{re.escape(module_name)}\b", original_module_text)
    if not m:
        return ""
    idx = _skip_ws(original_module_text, m.end())
    if idx >= len(original_module_text) or original_module_text[idx] != "#":
        return ""
    close = find_hash_paren_close(original_module_text, idx)
    return original_module_text[idx: close + 1]


def _stub_module_text(module: RtlModule, param_header: str) -> str:
    port_decls: list[str] = []
    for p in module.ports:
        rng = p.range_str()
        rng_sp = f"{rng} " if rng else ""
        direction = "input" if p.direction == PortDirection.INPUT else \
            ("inout" if p.direction == PortDirection.INOUT else "output")
        port_decls.append(f"    {direction} wire {rng_sp}{p.name}")

    # A single, output-unrelated dummy assignment keeps this module from
    # being treated as an (implicit or explicit) blackbox -- see this
    # module's docstring for why that matters. Ties to the first input if
    # one exists (true of every real submodule this is meant for), else a
    # constant, so the stub is self-contained either way.
    first_input = next((p.name for p in module.ports if p.direction == PortDirection.INPUT), None)
    keepalive_rhs = first_input if first_input else "1'b0"

    param_header_sp = f" {param_header}" if param_header else ""
    return (
        f"module {module.name}{param_header_sp} (\n"
        + ",\n".join(port_decls) + "\n"
        f");\n"
        f"    wire _bb_keepalive = {keepalive_rhs};\n"
        f"    // Every output above is deliberately left UNDRIVEN -- see\n"
        f"    // blackbox.py's module docstring. Do not add drivers here.\n"
        f"endmodule\n"
    )


_MEM_ARRAY_DECL = re.compile(
    r"\breg\s*(?:\[[^\]]*\])?\s*\w+\s*\[\s*([^\]:]+?)\s*:\s*([^\]]+?)\s*\]\s*;"
)
_WIDE_ARITH = re.compile(r"\w\s*\*\s*\w|\w\s*/\s*\w")


@dataclass
class BlackboxCandidate:
    module_name: str
    reason: str  # "large_memory" / "wide_arithmetic" / "large_module"
    detail: str
    score: int  # higher = stronger candidate


def _estimate_array_entries(lo: str, hi: str) -> Optional[int]:
    try:
        return abs(int(lo.strip(), 0) - int(hi.strip(), 0)) + 1
    except ValueError:
        return None  # a parameter name or expression, not a literal -- can't size it statically


def recommend_blackbox_candidates(module: RtlModule, rtl_source: str) -> List[BlackboxCandidate]:
    """Rank the target module's own DIRECTLY-instantiated submodules
    (defined in this same source text) as black-boxing candidates.

    This is meant to fire only AFTER a property's proof comes back
    genuinely inconclusive (TIMEOUT/UNKNOWN) -- black-box only once a
    proof is actually stuck, never pre-emptively, matching the trigger
    real formal verification teams use in practice, not a guess: Siemens'
    own Questa formal team publishes this exact framing --
    "when big counters and memories are in the active logic cone of an
    assertion that keeps coming up as inconclusive" -- as the textbook
    signal to reach for memory/register abstraction (see README's
    "Formal verification: engines, fallback, and honesty" section for the
    full citation trail this was researched from before writing this).

    Ranking, highest-priority first -- each backed by a documented,
    widely-cited real pattern, not an arbitrary guess:

      1. `large_memory` -- a submodule declaring a sizable unpacked
         register/memory array (`reg [W-1:0] mem [DEPTH-1:0];`). Large
         stateful arrays are the single most consistently cited trigger
         across industry write-ups (Siemens Verification Horizons,
         SemiWiki, lubis-eda) for a proof getting stuck, since a solver
         has to reason about every entry's own state. A parameterized
         bound that can't be resolved to a literal number is still
         flagged (conservatively, since it might genuinely be large) but
         ranked below a confirmed-large numeric one.
      2. `wide_arithmetic` -- a submodule containing a `*` or `/`
         operator. Bit-level reasoning about wide multipliers and
         dividers is long-documented in the arithmetic-circuit-
         verification literature as exponentially harder for BDD/SAT-
         based tools as operand width grows, independent of whether the
         specific property actually needs the arithmetic result's exact
         value.
      3. `large_module` -- otherwise, larger instantiated submodules (by
         body size) before smaller ones, the generic "complex modules"
         framing used across these same industry sources as a last-resort
         proxy when neither of the above applies.

    Only modules `module` actually instantiates (and that are defined in
    `rtl_source`) are considered: a solver's own automatic cone-of-
    influence reduction already discards genuinely disconnected logic for
    free (confirmed empirically in this project's own black-boxing
    testing -- see this module's own docstring above), so a module this
    scan can't even see being instantiated isn't a useful black-boxing
    target regardless of its own internal complexity.
    """
    clean_source = _strip_comments(rtl_source)
    module_body = _extract_module_body(clean_source, module.name)
    defined_modules = set(re.findall(r"\bmodule\s+(\w+)\b", rtl_source)) - {module.name}
    instantiations = _find_instantiations(module_body, defined_modules)

    seen: Set[str] = set()
    candidates: List[BlackboxCandidate] = []
    for inst in instantiations:
        if inst.module_name in seen:
            continue
        seen.add(inst.module_name)
        try:
            callee_body = _extract_module_body(clean_source, inst.module_name)
        except Exception:
            continue

        mem_matches = list(_MEM_ARRAY_DECL.finditer(callee_body))
        if mem_matches:
            sizes = [
                e for e in (
                    _estimate_array_entries(m.group(1), m.group(2)) for m in mem_matches
                ) if e is not None
            ]
            if sizes and max(sizes) >= 16:
                best = max(sizes)
                candidates.append(BlackboxCandidate(
                    module_name=inst.module_name, reason="large_memory",
                    detail=(
                        f"Contains a memory/register-array declaration with ~{best} entries -- "
                        "large stateful arrays are the textbook trigger for formal tools getting "
                        "stuck (see this function's own docstring for the sourced citations)."
                    ),
                    score=3_000_000 + best,
                ))
                continue
            if not sizes:
                candidates.append(BlackboxCandidate(
                    module_name=inst.module_name, reason="large_memory",
                    detail=(
                        "Contains a memory/register-array declaration with a parameterized size "
                        "(exact entry count not statically determined) -- flagged as a possible, "
                        "not confirmed, large-memory candidate; check manually before trusting "
                        "this ranking blindly."
                    ),
                    score=1_500_000,
                ))
                continue
            # else: numeric but small (<16 entries, e.g. a synchronizer's
            # own shift-register array) -- not a meaningful memory
            # candidate, fall through to the checks below.

        if _WIDE_ARITH.search(callee_body):
            candidates.append(BlackboxCandidate(
                module_name=inst.module_name, reason="wide_arithmetic",
                detail=(
                    "Contains a multiply/divide operator -- bit-level reasoning about wide "
                    "arithmetic is a long-documented hard case for SAT/BDD-based formal tools, "
                    "independent of whether this property needs the exact arithmetic result."
                ),
                score=2_000_000 + len(callee_body),
            ))
            continue

        candidates.append(BlackboxCandidate(
            module_name=inst.module_name, reason="large_module",
            detail=(
                f"No memory array or wide arithmetic detected; ranked by body size alone "
                f"({len(callee_body)} characters) as a last-resort complexity proxy."
            ),
            score=len(callee_body),
        ))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def generate_blackboxed_rtl(rtl_source: str, module_names: list[str]) -> str:
    """Return a new RTL source text with each named module's definition
    replaced by an undriven-output stub of the same name and port list.

    Every OTHER module in `rtl_source` (including whatever instantiates
    the blackboxed one) is left completely untouched -- an instantiation
    site needs no changes, since the stub's port list and connections are
    identical to the original.
    """
    result = rtl_source
    for name in module_names:
        # Re-locate on each iteration since prior replacements shift offsets.
        start, end = _find_module_span(result, name)
        original_module_text = result[start:end]
        module = analyze_rtl(original_module_text, top_module=name)
        param_header = _extract_param_header(original_module_text, name)
        result = result[:start] + _stub_module_text(module, param_header) + result[end:]
    return result
