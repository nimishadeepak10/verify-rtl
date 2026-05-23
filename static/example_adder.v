// 2-bit adder example RTL
module adder_2bit (
    input  wire [1:0] a,
    input  wire [1:0] b,
    output wire [2:0] sum
);
    assign sum = a + b;
endmodule
