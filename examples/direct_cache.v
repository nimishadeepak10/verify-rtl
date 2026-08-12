// Direct-mapped, write-through, write-no-allocate cache: 4 lines, 1 byte
// per line, 8-bit address split as {tag[5:0], index[1:0]}. The next rung
// after divider8.v/multiplier32.v -- tag/valid arrays plus a multi-cycle
// miss-fill FSM talking to an external memory interface (mem_req/mem_we/
// mem_addr/mem_wdata -> mem_rdata/mem_ready), which is itself a new kind
// of complexity (an unconstrained external responder) beyond anything
// tested so far.
//
// Request fields (index/tag/we/wdata) are latched into r_index/r_tag/
// r_we/r_wdata at accept time and used throughout the FILL state instead
// of re-reading the live addr/we/wdata inputs -- this is deliberate,
// learned directly from Stage 5's divider finding, where a same-cycle
// property compared against a live input port instead of an internally
// captured value and produced a spurious counterexample. Avoiding that
// bug class by construction here, not just in property wording.
//
// hit and ready are always asserted together (same cycle), including on
// the fast read-hit path and the delayed FILL-completion path, so "hit
// implies ready" is a real same-cycle invariant, not just an interface
// convention.
module direct_cache (
    input  wire       clk,
    input  wire       rst_n,
    // CPU-side request interface
    input  wire        req,
    input  wire        we,
    input  wire [7:0]  addr,
    input  wire [7:0]  wdata,
    output reg  [7:0]  rdata,
    output reg          hit,
    output reg          ready,
    // memory-side interface (external backing store, not modeled here --
    // mem_ready/mem_rdata are free inputs from the environment/testbench)
    output reg          mem_req,
    output reg          mem_we,
    output reg  [7:0]  mem_addr,
    output reg  [7:0]  mem_wdata,
    input  wire [7:0]  mem_rdata,
    input  wire        mem_ready
);
    localparam IDLE = 1'b0;
    localparam FILL = 1'b1;
    reg cstate;

    reg [5:0] tag_arr [0:3];
    reg [7:0] data_arr [0:3];
    reg        valid_arr [0:3];

    reg [1:0] r_index;
    reg [5:0] r_tag;
    reg        r_we;
    reg        r_hit;

    wire [1:0] index   = addr[1:0];
    wire [5:0] tag      = addr[7:2];
    wire       tag_hit  = valid_arr[index] && (tag_arr[index] == tag);

    integer i;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cstate    <= IDLE;
            ready     <= 1'b0;
            hit       <= 1'b0;
            mem_req   <= 1'b0;
            mem_we    <= 1'b0;
            mem_addr  <= 8'd0;
            mem_wdata <= 8'd0;
            rdata     <= 8'd0;
            r_index   <= 2'd0;
            r_tag     <= 6'd0;
            r_we      <= 1'b0;
            r_hit     <= 1'b0;
            for (i = 0; i < 4; i = i + 1) begin
                valid_arr[i] <= 1'b0;
                tag_arr[i]   <= 6'd0;
                data_arr[i]  <= 8'd0;
            end
        end else begin
            ready <= 1'b0;
            hit   <= 1'b0;
            case (cstate)
                IDLE: begin
                    if (req) begin
                        r_index <= index;
                        r_tag   <= tag;
                        r_we    <= we;
                        if (we) begin
                            mem_req   <= 1'b1;
                            mem_we    <= 1'b1;
                            mem_addr  <= addr;
                            mem_wdata <= wdata;
                            if (tag_hit)
                                data_arr[index] <= wdata;
                            r_hit  <= tag_hit;
                            cstate <= FILL;
                        end else if (tag_hit) begin
                            rdata  <= data_arr[index];
                            hit    <= 1'b1;
                            ready  <= 1'b1;
                            cstate <= IDLE;
                        end else begin
                            mem_req  <= 1'b1;
                            mem_we   <= 1'b0;
                            mem_addr <= addr;
                            r_hit    <= 1'b0;
                            cstate   <= FILL;
                        end
                    end
                end
                FILL: begin
                    if (mem_ready) begin
                        mem_req <= 1'b0;
                        if (!r_we) begin
                            data_arr[r_index]  <= mem_rdata;
                            tag_arr[r_index]   <= r_tag;
                            valid_arr[r_index] <= 1'b1;
                            rdata <= mem_rdata;
                        end
                        hit    <= r_hit;
                        ready  <= 1'b1;
                        cstate <= IDLE;
                    end
                end
            endcase
        end
    end
endmodule
