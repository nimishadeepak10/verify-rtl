// Moore FSM: two-state traffic light controller (sequential + reset)
module traffic_light_fsm (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       go,
    output wire [1:0] light
);
    localparam S_RED   = 2'd0;
    localparam S_GREEN = 2'd1;

    reg [1:0] state;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            state <= S_RED;
        else begin
            case (state)
                S_RED:   state <= go ? S_GREEN : S_RED;
                S_GREEN: state <= S_RED;
                default: state <= S_RED;
            endcase
        end
    end

    assign light = state;
endmodule
