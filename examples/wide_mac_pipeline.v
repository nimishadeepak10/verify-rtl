// A genuinely large, deep multiply-accumulate pipeline -- real datapath a
// chip would actually use (e.g. part of a DSP/filter block), NOT dead
// code. Its output is wired to a real top-level port in big_soc_wrapper.v
// so yosys's own optimizer can't quietly strip it away as unused --
// exactly why this is a meaningful black-boxing target rather than a
// strawman: a real formal run against the wrapper genuinely has to
// elaborate and reason about all of this state unless told not to.
//
// 24 pipeline stages, each holding a 64-bit accumulator plus two 32-bit
// operands in flight -- a state space large enough to make PDR/BMC work
// noticeably harder, deliberately, while being functionally UNRELATED to
// arbiter4's grant/request signals (no connection between the two blocks
// anywhere in big_soc_wrapper.v).
module wide_mac_pipeline #(
    parameter integer STAGES = 24
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [31:0] a,
    input  wire [31:0] b,
    output wire [63:0] result
);
    reg [31:0] a_pipe [0:STAGES-1];
    reg [31:0] b_pipe [0:STAGES-1];
    reg [63:0] acc_pipe [0:STAGES-1];
    integer s;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (s = 0; s < STAGES; s = s + 1) begin
                a_pipe[s]   <= 32'd0;
                b_pipe[s]   <= 32'd0;
                acc_pipe[s] <= 64'd0;
            end
        end else begin
            a_pipe[0]   <= a;
            b_pipe[0]   <= b;
            acc_pipe[0] <= acc_pipe[STAGES-1] + (a * b);
            for (s = 1; s < STAGES; s = s + 1) begin
                a_pipe[s]   <= a_pipe[s-1];
                b_pipe[s]   <= b_pipe[s-1];
                acc_pipe[s] <= acc_pipe[s-1] + (a_pipe[s-1] * b_pipe[s-1]);
            end
        end
    end

    assign result = acc_pipe[STAGES-1];
endmodule
