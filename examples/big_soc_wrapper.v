// Synthetic "industrial-shaped" design for validating black-box-based
// design-size reduction: a small, easily-verified control block
// (arbiter4) sitting beside a large, genuinely-used datapath block
// (wide_mac_pipeline) it has no functional connection to whatsoever.
//
// The point this design exists to make: a property about arbiter4's
// grant signal is completely independent of wide_mac_pipeline's internal
// state, but a naive formal run still has to elaborate and reason about
// ALL of it, because wide_mac_pipeline's output is wired to a real,
// observable top-level port -- yosys's own dead-code elimination can't
// remove it, unlike an unused/dangling block would be. This is exactly
// the shape a real chip has: many real, live blocks, only a few of which
// matter to any one property being checked right now.
module big_soc_wrapper (
    input  wire        clk,
    input  wire        rst_n,

    input  wire [3:0]  req,
    output wire [3:0]  grant,

    input  wire [31:0] mac_a,
    input  wire [31:0] mac_b,
    output wire [63:0] mac_result
);
    // The arbiter and the MAC pipeline share a bus: while the MAC engine's
    // accumulator has anything in its top half (an arbitrary but REAL
    // condition on the actual computed value, not a dummy tie-off), new
    // grants are held off. This is exactly the shape that defeats a
    // solver's own automatic cone-of-influence reduction: grant's
    // correctness now genuinely depends on facts about wide_mac_pipeline's
    // 24-stage arithmetic, even though the ONE-HOT property being checked
    // doesn't care what value busy_block actually takes -- only that it
    // exists. That's precisely what makes black-boxing wide_mac_pipeline
    // for THIS property both safe and valuable, unlike two genuinely
    // disconnected blocks (which a solver already discards on its own).
    wire mac_busy = |mac_result[63:48];

    arbiter4 u_arbiter (
        .clk(clk), .rst_n(rst_n),
        .req(req), .busy_block(mac_busy), .grant(grant)
    );

    wide_mac_pipeline #(.STAGES(200)) u_mac (
        .clk(clk), .rst_n(rst_n),
        .a(mac_a), .b(mac_b), .result(mac_result)
    );
endmodule
