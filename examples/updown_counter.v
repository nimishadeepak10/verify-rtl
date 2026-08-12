// Saturating up/down counter with a small control FSM: 8-bit count with
// load/clear/enable/direction control, plus a registered status (IDLE /
// COUNTING / SATURATED). The "next rung" after sync_fifo.v's simple
// pointer arithmetic and traffic_light_fsm.v's simple state cycling --
// real arithmetic saturation boundaries (no wraparound at the edges)
// combined with real control-flow priority (clear beats load beats
// enable) and a status FSM whose correctness depends on both.
module updown_counter (
    input  wire       clk,
    input  wire       rst_n,
    input  wire        clear,
    input  wire        load,
    input  wire [7:0]  load_value,
    input  wire        enable,
    input  wire        up_down,   // 1 = count up, 0 = count down
    output reg  [7:0]  count,
    output wire        saturated,
    output reg  [1:0]  state      // 0=IDLE 1=COUNTING 2=SATURATED
);
    localparam IDLE      = 2'd0;
    localparam COUNTING  = 2'd1;
    localparam SATURATED = 2'd2;

    wire at_max = (count == 8'hFF);
    wire at_min = (count == 8'h00);
    assign saturated = enable && ((up_down && at_max) || (!up_down && at_min));

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            count <= 8'd0;
            state <= IDLE;
        end else if (clear) begin
            count <= 8'd0;
            state <= IDLE;
        end else if (load) begin
            count <= load_value;
            state <= COUNTING;
        end else if (enable) begin
            if (up_down && !at_max)
                count <= count + 8'd1;
            else if (!up_down && !at_min)
                count <= count - 8'd1;
            state <= saturated ? SATURATED : COUNTING;
        end
    end
endmodule
