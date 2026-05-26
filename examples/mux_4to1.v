module mux_4to1 (
    input  [3:0] d0, d1, d2, d3,
    input  [1:0] sel,
    output reg [3:0] y
);
    always @(*) begin
        if (sel == 2'b00) y = d0;
        else if (sel == 2'b01) y = d1;
        else if (sel == 2'b10) y = d2;
        else y = d3;
    end
endmodule
