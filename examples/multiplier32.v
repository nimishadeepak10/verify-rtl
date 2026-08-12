// 32x32 -> 64-bit unsigned combinational multiplier. Purely combinational,
// so per this project's own recommended_formal_config() rationale a
// single BMC step is already exhaustive -- COMPLETE regardless of width.
// The point of this design isn't completeness, it's wall-clock cost: a
// property like "product == a * b" forces the SAT solver to bit-blast a
// 32x32 multiplication equivalence check, which can be genuinely slow
// even though the proof is exhaustive in principle. Built specifically to
// see whether that produces a real TIMEOUT under a short budget --
// honestly testing Stage 1's inconclusive-verdict machinery against
// actual solver difficulty, not assuming it in advance.
module multiplier32 (
    input  wire [31:0] a,
    input  wire [31:0] b,
    output wire [63:0] product
);
    assign product = a * b;
endmodule
