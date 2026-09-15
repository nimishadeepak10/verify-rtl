// Single cache-line MESI FSM core, instantiated N times by
// mesi_multi_cache.v to build a multi-cache coherence formal model.
//
// This is examples/mesi_line.v's same state machine and encoding (I=0,
// S=1, E=2, M=3; snoop wins over a local request), but split out and
// extended with two things a single, standalone cache line doesn't need:
//
//   - `other_has_line`: whether some OTHER cache currently holds this
//     line, sampled combinationally the same cycle as a local read miss.
//     A real MESI cache needs this to decide Exclusive (sole owner) vs
//     Shared (someone else already has it) -- information a single-cache
//     model has no way to express, since by definition there IS no other
//     cache. mesi_line.v's local_req case for ST_I always goes to E
//     because, in isolation, it always looks like sole ownership.
//
//   - `issue_busrd` / `issue_busrdx`: whether this cache's local request
//     this cycle implies a bus transaction the other caches must see as a
//     snoop. Kept minimal on purpose -- E/M handle a local write silently
//     (no broadcast) since the cache is already the line's sole owner;
//     only a request from I or S needs to reach the bus.
module mesi_cache_core (
    input  wire       clk,
    input  wire       rst_n,
    // Local CPU-side request (held for one cycle when asserted)
    input  wire       cpu_read,
    input  wire       cpu_write,
    // Bus snoop from other caches' local requests this cycle
    input  wire       snoop_read,     // BusRd   -- another cache read-shared
    input  wire       snoop_readex,   // BusRdX  -- another cache read-exclusive / invalidate
    // Whether some OTHER cache currently holds this line (this cycle's
    // pre-transaction state) -- decides Exclusive vs Shared on a read miss
    input  wire       other_has_line,
    output reg  [1:0] state,
    // Bus transactions THIS cache's local request implies this cycle, fanned
    // out to every other cache as their snoop_read/snoop_readex inputs
    output wire       issue_busrd,
    output wire       issue_busrdx
);
    localparam [1:0] ST_I = 2'd0;
    localparam [1:0] ST_S = 2'd1;
    localparam [1:0] ST_E = 2'd2;
    localparam [1:0] ST_M = 2'd3;

    wire local_req = cpu_read | cpu_write;
    wire snoop     = snoop_read | snoop_readex;

    // A read from I always needs the bus (to learn whether anyone else has
    // it); a write from I or S needs the bus to invalidate everyone else.
    // A write from E or M is a silent upgrade/hit -- no broadcast, since
    // this cache is already the line's sole owner.
    assign issue_busrd  = local_req && cpu_read  && !cpu_write && (state == ST_I);
    assign issue_busrdx = local_req && cpu_write && ((state == ST_I) || (state == ST_S));

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= ST_I;
        end else if (snoop) begin
            // Snoop wins over local request this cycle (mirrors
            // mesi_line.v; also structurally guaranteed at the
            // mesi_multi_cache.v level, since a cache never masters the
            // bus and receives a snoop in the same cycle).
            case (state)
                ST_E: state <= snoop_readex ? ST_I : ST_S;
                ST_M: state <= snoop_readex ? ST_I : ST_S;
                ST_S: state <= snoop_readex ? ST_I : ST_S;
                default: state <= ST_I;
            endcase
        end else if (local_req) begin
            case (state)
                ST_I: state <= cpu_write ? ST_M : (other_has_line ? ST_S : ST_E);
                ST_S: state <= cpu_write ? ST_M : ST_S;
                ST_E: state <= cpu_write ? ST_M : ST_E;
                ST_M: state <= ST_M;
                default: state <= ST_I;
            endcase
        end
    end
endmodule
