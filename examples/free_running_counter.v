// 8-bit free-running up-counter, no saturation — wraps at 256.
// Used to demonstrate the difference between bounded (BMC) and unbounded
// (PDR) formal proof: "count < 100" is true for the first 100 cycles and
// false afterward, so a shallow BMC depth can wrongly report PASS simply
// by never looking far enough, while PDR proves/falsifies for all time.
module free_running_counter (
    input  wire       clk,
    input  wire       rst_n,
    output reg  [7:0] count
);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            count <= 8'd0;
        else
            count <= count + 8'd1;
    end
endmodule
