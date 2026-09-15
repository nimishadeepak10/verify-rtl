// Single cache-line MESI controller (teaching example for formal verification).
//
// States (2-bit one-hot style encoding):
//   I = 2'd0  Invalid
//   S = 2'd1  Shared (clean, may be cached elsewhere)
//   E = 2'd2  Exclusive (clean, this cache alone)
//   M = 2'd3  Modified (dirty, this cache alone)
//
// One local request and one snoop may be presented per cycle; the FSM
// prioritizes snoops over local CPU ops (standard simplification).
module mesi_line (
    input  wire       clk,
    input  wire       rst_n,
    // Local CPU-side request (held for one cycle when asserted)
    input  wire       cpu_read,
    input  wire       cpu_write,
    // Bus snoop from other cores
    input  wire       snoop_read,    // BusRd  — other core read-shared
    input  wire       snoop_readex,  // BusRdX — other core read-exclusive / invalidate
    output reg  [1:0] state
);
    localparam [1:0] ST_I = 2'd0;
    localparam [1:0] ST_S = 2'd1;
    localparam [1:0] ST_E = 2'd2;
    localparam [1:0] ST_M = 2'd3;

    wire local_req = cpu_read | cpu_write;
    wire snoop     = snoop_read | snoop_readex;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= ST_I;
        end else if (snoop) begin
            // Snoop wins over local request this cycle
            case (state)
                ST_E: state <= snoop_readex ? ST_I : ST_S;
                ST_M: state <= snoop_readex ? ST_I : ST_S;
                ST_S: state <= snoop_readex ? ST_I : ST_S;
                default: state <= ST_I;
            endcase
        end else if (local_req) begin
            case (state)
                ST_I: state <= cpu_write ? ST_M : ST_E;
                ST_S: state <= cpu_write ? ST_M : ST_S;
                ST_E: state <= cpu_write ? ST_M : ST_E;
                ST_M: state <= ST_M;
                default: state <= ST_I;
            endcase
        end
    end

`ifdef FORMAL
    // --- Environment assumptions (inputs the design may rely on) ---
    initial assume(!rst_n);

    always @(posedge clk) begin
        if (rst_n) begin
            // At most one kind of local request per cycle
            assume(!(cpu_read && cpu_write));
            // At most one snoop type per cycle
            assume(!(snoop_read && snoop_readex));
        end
    end

    // --- Safety invariants ---
    always @(posedge clk) begin
        if (rst_n) begin
            valid_encoding: assert(state <= ST_M);
        end
    end

    // --- Reachability covers (run with mode cover in .sby) ---
    always @(posedge clk) begin
        if (rst_n) begin
            reach_shared:    cover(state == ST_S);
            reach_exclusive: cover(state == ST_E);
            reach_modified:  cover(state == ST_M);
        end
    end
`endif
endmodule
