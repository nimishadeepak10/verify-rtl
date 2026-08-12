// 8-bit / 8-bit restoring binary divider, 8 cycles per division. Complex
// math, and genuinely iterative -- the first design in this project whose
// correctness claim (quotient*divisor + remainder == dividend) involves
// nonlinear arithmetic accumulated across many cycles, not checkable in
// one clock edge. Deliberately built as a stress test for whether the
// formal pipeline stays honest under real difficulty rather than another
// all-green example (see scripts/test_divider_suggestions.py).
module divider8 (
    input  wire       clk,
    input  wire       rst_n,
    input  wire        start,
    input  wire [7:0]  dividend,
    input  wire [7:0]  divisor,
    output reg  [7:0]  quotient,
    output reg  [7:0]  remainder,
    output reg          busy,
    output reg          done,
    output reg          div_by_zero
);
    reg [2:0] step;
    reg [7:0] rem_reg;
    reg [7:0] quo_reg;
    reg [7:0] div_reg;

    wire [7:0] shifted_rem = {rem_reg[6:0], quo_reg[7]};
    wire [8:0] trial = {1'b0, shifted_rem} - {1'b0, div_reg};
    wire       fits  = !trial[8]; // no borrow => divisor fits into shifted remainder

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            busy <= 1'b0;
            done <= 1'b0;
            div_by_zero <= 1'b0;
            quotient <= 8'd0;
            remainder <= 8'd0;
            step <= 3'd0;
            rem_reg <= 8'd0;
            quo_reg <= 8'd0;
            div_reg <= 8'd0;
        end else if (start && !busy) begin
            if (divisor == 8'd0) begin
                div_by_zero <= 1'b1;
                done <= 1'b1;
                busy <= 1'b0;
                quotient <= 8'd0;
                remainder <= 8'd0;
            end else begin
                div_by_zero <= 1'b0;
                done <= 1'b0;
                busy <= 1'b1;
                rem_reg <= 8'd0;
                quo_reg <= dividend;
                div_reg <= divisor;
                step <= 3'd0;
            end
        end else if (busy) begin
            if (fits) begin
                rem_reg <= trial[7:0];
                quo_reg <= {quo_reg[6:0], 1'b1};
            end else begin
                rem_reg <= shifted_rem;
                quo_reg <= {quo_reg[6:0], 1'b0};
            end
            if (step == 3'd7) begin
                busy <= 1'b0;
                done <= 1'b1;
                quotient <= fits ? {quo_reg[6:0], 1'b1} : {quo_reg[6:0], 1'b0};
                remainder <= fits ? trial[7:0] : shifted_rem;
            end else begin
                step <= step + 3'd1;
            end
        end
    end
endmodule
