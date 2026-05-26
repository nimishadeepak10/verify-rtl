module priority_encoder_4bit (
    input  [3:0] req,
    output reg [1:0] grant,
    output reg valid
);
    always @(*) begin
        casez (req)
            4'b1???: begin grant = 2'd3; valid = 1'b1; end
            4'b01??: begin grant = 2'd2; valid = 1'b1; end
            4'b001?: begin grant = 2'd1; valid = 1'b1; end
            4'b0001: begin grant = 2'd0; valid = 1'b1; end
            default: begin grant = 2'd0; valid = 1'b0; end
        endcase
    end
endmodule
