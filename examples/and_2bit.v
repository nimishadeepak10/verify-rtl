// 2-bit AND example
module and_2bit (
    input  wire [1:0] a,
    input  wire [1:0] b,
    output wire [1:0] y
);
    assign y = a & b;
endmodule
