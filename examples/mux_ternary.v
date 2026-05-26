module mux_ternary (
    input  [7:0] a, b,
    input  sel,
    output [7:0] y
);
    assign y = sel ? a : b;
endmodule
