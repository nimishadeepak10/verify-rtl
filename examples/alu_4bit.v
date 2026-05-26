module alu_4bit (
    input  [3:0] a, b,
    input  [2:0] opcode,
    output reg [3:0] result,
    output reg       zero
);
    always @(*) begin
        case (opcode)
            3'b000: result = a + b;
            3'b001: result = a - b;
            3'b010: result = a & b;
            3'b011: result = a | b;
            3'b100: result = a ^ b;
            3'b101: result = ~a;
            3'b110: result = a << 1;
            3'b111: result = a >> 1;
        endcase
        zero = (result == 4'b0);
    end
endmodule
