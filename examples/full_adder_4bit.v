module full_adder_4bit (
    input  [3:0] a, b,
    input  c_in,
    output c_out,
    output [3:0] sum
);
    assign {c_out, sum} = a + b + c_in;
endmodule
