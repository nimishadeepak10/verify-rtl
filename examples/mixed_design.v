module mixed_design (
    input  [3:0] a, b,
    input  [1:0] mode,
    output reg [3:0] result,
    output any_input_nonzero
);
    assign any_input_nonzero = (|a) | (|b);

    always @(*) begin
        case (mode)
            2'b00: result = a + b;
            2'b01: result = (a > b) ? a : b;
            2'b10: result = {a[1:0], b[1:0]};
            default: result = 4'b0;
        endcase
    end
endmodule
