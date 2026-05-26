module signed_lt (
    input  signed [7:0] a, b,
    output a_less_than_b,
    output a_negative
);
    assign a_less_than_b = ($signed(a) < $signed(b));
    assign a_negative = ($signed(a) < 0);
endmodule
