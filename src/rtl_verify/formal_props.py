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
hand-written string (today) or an LLM-generated one later (Phase 2) —
only the `expr` strings change, the wiring stays the same.
"""

from __future__ import annotations

from typing import Sequence, Tuple

from .analyzer import PortDirection, RtlModule

Property = Tuple[str, str]  # (name, boolean_expression) using the DUT's own port names


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
        raise ValueError("Need at least one (name, expression) property")
    clk = clock_port or module.clock_port
    if not clk:
        raise ValueError(
            f"'{module.name}' has no detected clock port; pass clock_port explicitly"
        )

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

    assert_lines = [f"        {name}: assert ({expr});" for name, expr in properties]
    conns_str = ",\n        ".join(conns)

    # Without this, BMC is free to start sequential state in a value the RTL's
    # own logic never actually produces (registers have no reset value until
    # the first clock edge applies it) — a classic false counterexample, not
    # a real bug. Assuming reset is asserted at time 0 rules that out, the
    # same way simulation always implicitly goes through reset first.
    reset_assume = ""
    if module.reset_port:
        reset_expr = (
            f"!{module.reset_port}" if module.reset_active_low else module.reset_port
        )
        reset_assume = f"""    initial begin
        assume({reset_expr});
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
{reset_assume}    always @(posedge {clk}) begin
{chr(10).join(assert_lines)}
    end
`endif
endmodule
"""
