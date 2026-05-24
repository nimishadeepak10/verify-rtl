// 4-bit ALU — smaller input space for exhaustive simulation (512 vectors)
module alu_4bit (
    input  wire [3:0] a,
    input  wire [3:0] b,
    input  wire [1:0] opcode,
    output wire [3:0] result
);
    assign result = (opcode == 2'd0) ? (a + b) :
                    (opcode == 2'd1) ? (a & b) :
                    (opcode == 2'd2) ? (a ^ b) : (a - b);
endmodule
