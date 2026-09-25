// Small 4-way round-robin bus arbiter -- the block we actually want to
// verify. Deliberately tiny and simple: a real formal engineer's actual
// target here would be exactly this kind of control logic, not the wide
// datapath it sits beside (see big_soc_wrapper.v / wide_mac_pipeline.v).
module arbiter4 (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [3:0] req,
    input  wire       busy_block,  // e.g. a shared resource (see wide_mac_pipeline.v) says "not now"
    output reg  [3:0] grant
);
    reg [1:0] last_granted;

    function [1:0] next_after(input [1:0] p);
        next_after = p + 2'd1;
    endfunction

    reg [1:0] pick;
    integer i;
    always @* begin
        pick = last_granted;
        for (i = 0; i < 4; i = i + 1) begin
            reg [1:0] cand;
            cand = next_after(last_granted) + i[1:0];
            if (req[cand] && pick == last_granted)
                pick = cand;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            grant        <= 4'd0;
            last_granted <= 2'd3;
        end else if (|req && !busy_block) begin
            grant        <= (4'd1 << pick);
            last_granted <= pick;
        end else begin
            grant <= 4'd0;
        end
    end
endmodule
