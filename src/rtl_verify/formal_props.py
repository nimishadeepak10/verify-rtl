"""Minimal SVA property-file generator for the SymbiYosys formal backend.

Builds a standalone wrapper module that instantiates the DUT and checks
the given boolean properties on every clock edge — it never edits the DUT
source file. A `bind`-based checker was tried first, but yosys's built-in
Verilog frontend silently drops standalone `bind` statements (confirmed by
running it: the bound module gets logged as an unused module and removed
during hierarchy analysis, so nothing was ever actually checked). Plain
instantiation has no such gap — it's ordinary Verilog, not a formal-only
construct — so it's the reliable choice here.

This keeps the same output shape whether a property comes from a
hand-written string or an LLM-generated one (Phase 2) — only the `expr`
strings change, the wiring stays the same. Each property also carries a
`kind` — assert/assume/cover — since these mean genuinely different
things to SymbiYosys, not just different keywords: assert/assume run
together under the existing bmc/prove modes below, but a cover only
means anything under a separate `mode="cover"` run (see
recommended_formal_config and backends/symbiyosys.py).

Sequential (clocked) and combinational (clockless) DUTs both work:
sequential properties are checked every clock edge with a reset
assumption at time 0; combinational properties are checked on every
input change via `always @(*)`, since there's no clock and no state to
assume a reset for. `recommended_formal_config()` picks a matching
SymbiYosys mode/depth for either case from what the analyzer already
knows about the design.
"""

from __future__ import annotations

from typing import Sequence, Tuple

from .analyzer import PortDirection, RtlModule

# (name, boolean_expression, kind) using the DUT's own port names.
# kind is "assert" (design must guarantee), "assume" (environment/input
# constraint the design may rely on), or "cover" (reachability claim —
# checked for meaning only under mode="cover"; see backends/symbiyosys.py).
Property = Tuple[str, str, str]

_KEYWORD = {"assert": "assert", "assume": "assume", "cover": "cover"}


def generate_formal_wrapper(
    module: RtlModule,
    properties: Sequence[Property],
    clock_port: str | None = None,
) -> str:
    """Return SV text: a wrapper module instantiating the DUT plus its properties.

    Pass the wrapper's own name (``f"{module.name}_formal_top"``) as ``top``
    to SymbiYosysBackend.run(), alongside both this file and the original
    RTL file.
    """
    if not properties:
        raise ValueError("Need at least one (name, expression, kind) property")
    for name, _expr, kind in properties:
        if kind not in _KEYWORD:
            raise ValueError(f"'{name}': kind must be assert/assume/cover, got {kind!r}")
    clk = clock_port or module.clock_port

    wrapper_name = f"{module.name}_formal_top"

    input_decls: list[str] = []
    output_decls: list[str] = []
    conns: list[str] = []
    top_ports: list[str] = []
    for p in module.ports:
        rng = p.range_str()
        rng_sp = f"{rng} " if rng else ""
        conns.append(f".{p.name}({p.name})")
        if p.direction == PortDirection.INPUT:
            input_decls.append(f"    input {rng_sp}{p.name};")
            top_ports.append(p.name)
        else:
            output_decls.append(f"    wire {rng_sp}{p.name};")

    assert_lines = [
        f"        {name}: {_KEYWORD[kind]} ({expr});" for name, expr, kind in properties
    ]
    conns_str = ",\n        ".join(conns)

    if clk:
        # Without this, BMC is free to start sequential state in a value the
        # RTL's own logic never actually produces (registers have no reset
        # value until the first clock edge applies it) — a classic false
        # counterexample, not a real bug. Assuming reset is asserted at time
        # 0 rules that out, the same way simulation always implicitly goes
        # through reset first.
        reset_assume = ""
        if module.reset_port:
            reset_expr = (
                f"!{module.reset_port}" if module.reset_active_low else module.reset_port
            )
            reset_assume = f"""    initial begin
        assume({reset_expr});
    end
"""
        check_block = f"""{reset_assume}    always @(posedge {clk}) begin
{chr(10).join(assert_lines)}
    end
"""
    else:
        # No clock, no state — check on every input change instead of every
        # clock edge. No reset assumption needed: there's nothing to reset.
        check_block = f"""    always @(*) begin
{chr(10).join(assert_lines)}
    end
"""

    return f"""// Auto-generated formal wrapper for {module.name} — does not modify the DUT source
module {wrapper_name} (
    {", ".join(top_ports)}
);
{chr(10).join(input_decls)}
{chr(10).join(output_decls)}

    {module.name} dut (
        {conns_str}
    );

`ifdef FORMAL
{check_block}`endif
endmodule
"""


def recommended_formal_config(module: RtlModule, kind: str = "assert") -> dict:
    """Pick a SymbiYosys mode/engine/depth from what the analyzer already knows.

    For assert/assume:

    Combinational designs have no time-varying state, so a single BMC step
    already checks every input combination exhaustively via SAT — that's a
    complete proof already, not a bounded approximation; there's no "later
    cycle" it could have missed.

    Sequential designs get PDR (`mode prove`, `abc pdr`) — an unbounded
    proof, not a fixed number of unrolled cycles. This matters concretely:
    a plain BMC check at a shallow depth can report PASS on a property
    that's only true for the first N cycles and false afterward, simply
    because it never looked far enough (see
    scripts/test_formal_unbounded.py for a worked example). PDR searches
    for a proof or a genuine counterexample regardless of how far away it
    is, so `depth` isn't part of this config for sequential designs.

    For cover: always `mode="cover"` regardless of sequential/combinational
    — confirmed by running it that `mode="prove"` (PDR) silently folds a
    `cover` statement into the proof as a constraint instead of actually
    checking reachability ("the last N outputs are interpreted as
    constraints" in the ABC log), so a cover only means something under
    `mode="cover"`. Depth uses the same FSM-state-derived heuristic as the
    sequential assert case, since reachability can need more steps than a
    shallow default; combinational cover still only needs depth=1, for the
    same single-step-is-exhaustive reason asserts do.
    """
    if kind == "cover":
        if not module.is_sequential:
            return {"mode": "cover", "engine": "smtbmc", "depth": 1}
        depth = max(10, len(module.states) * 4) if module.states else 20
        return {"mode": "cover", "engine": "smtbmc", "depth": depth}
    if not module.is_sequential:
        return {"mode": "bmc", "engine": "smtbmc", "depth": 1}
    return {"mode": "prove", "engine": "abc pdr", "depth": 0}
