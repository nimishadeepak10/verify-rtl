// 8-bit ALU example for waveform viewer testing
module alu_8bit (
    input  wire [7:0] a,
    input  wire [7:0] b,
    input  wire [2:0] opcode,
    output wire [7:0] result
);
    assign result = (opcode == 3'd0) ? (a + b) :
                    (opcode == 3'd1) ? (a & b) :
                    (opcode == 3'd2) ? (a ^ b) :
                    (opcode == 3'd3) ? (a | b) :
                    (opcode == 3'd4) ? (a - b) : 8'd0;
endmodule
