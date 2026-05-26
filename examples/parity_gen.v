module parity_gen (
    input  [7:0] data,
    output odd_par,
    output even_par,
    output any_set,
    output all_set
);
    assign odd_par  = ^data;
    assign even_par = ~^data;
    assign any_set  = |data;
    assign all_set  = &data;
endmodule
