// 2-bit OR example
module or_2bit (
    input  wire [1:0] a,
    input  wire [1:0] b,
    output wire [1:0] y
);
    assign y = a | b;
endmodule
