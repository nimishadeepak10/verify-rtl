// Formal model of N caches, each running mesi_cache_core.v, sharing ONE
// cache line over a single snooping bus -- the standard simplification for
// proving MESI's actual coherence invariants (mutual exclusion of
// Modified, no stale reads after invalidation) rather than cache capacity
// or replacement behavior. A real multi-line, multi-address cache still
// has to satisfy exactly these same per-line invariants; nothing here is
// specific to having only one line.
//
// Bus model: at most one cache is granted the bus (may drive a local CPU
// request) per cycle -- see the `assume` below. The granted cache's core
// computes any bus transaction its request implies (issue_busrd /
// issue_busrdx); every OTHER cache receives that transaction as a snoop
// THIS SAME cycle -- real snooping-bus semantics, not a queued/async
// model. `other_has_line` similarly fans in each cache's view of whether
// any other cache currently holds the line, needed to decide Exclusive
// (sole owner) vs Shared on a read miss.
module mesi_multi_cache #(
    parameter integer N = 3
) (
    input  wire         clk,
    input  wire         rst_n,
    input  wire [N-1:0] cpu_read,
    input  wire [N-1:0] cpu_write
);
    localparam [1:0] ST_I = 2'd0;
    localparam [1:0] ST_S = 2'd1;
    localparam [1:0] ST_E = 2'd2;
    localparam [1:0] ST_M = 2'd3;

    wire [1:0] state        [0:N-1];
    wire       issue_busrd  [0:N-1];
    wire       issue_busrdx [0:N-1];
    wire [N-1:0] other_has_line;
    wire [N-1:0] snoop_read_in;
    wire [N-1:0] snoop_readex_in;

    genvar gi, gj;

    generate
        for (gi = 0; gi < N; gi = gi + 1) begin : G_FANIN
            wire [N-1:0] has_mask, rd_mask, rdx_mask;
            for (gj = 0; gj < N; gj = gj + 1) begin : G_FANIN_J
                assign has_mask[gj] = (gj == gi) ? 1'b0 : (state[gj] != ST_I);
                assign rd_mask[gj]  = (gj == gi) ? 1'b0 : issue_busrd[gj];
                assign rdx_mask[gj] = (gj == gi) ? 1'b0 : issue_busrdx[gj];
            end
            assign other_has_line[gi]  = |has_mask;
            assign snoop_read_in[gi]   = |rd_mask;
            assign snoop_readex_in[gi] = |rdx_mask;
        end
    endgenerate

    generate
        for (gi = 0; gi < N; gi = gi + 1) begin : G_CACHE
            mesi_cache_core core (
                .clk(clk),
                .rst_n(rst_n),
                .cpu_read(cpu_read[gi]),
                .cpu_write(cpu_write[gi]),
                .snoop_read(snoop_read_in[gi]),
                .snoop_readex(snoop_readex_in[gi]),
                .other_has_line(other_has_line[gi]),
                .state(state[gi]),
                .issue_busrd(issue_busrd[gi]),
                .issue_busrdx(issue_busrdx[gi])
            );
        end
    endgenerate

`ifdef FORMAL
    // ================= Environment assumptions =================
    initial assume(!rst_n);

    wire [N-1:0] requesting = cpu_read | cpu_write;

    genvar ga;
    generate
        for (ga = 0; ga < N; ga = ga + 1) begin : G_ASSUME_PER_CACHE
            always @(posedge clk) begin
                if (rst_n) begin
                    // A1: each cache issues at most one kind of local
                    // request per cycle (mirrors mesi_line.v's own
                    // per-cache assumption).
                    assume(!(cpu_read[ga] && cpu_write[ga]));
                end
            end
        end
    endgenerate

    always @(posedge clk) begin
        if (rst_n) begin
            // A2: single shared bus -- at most one cache is granted the
            // bus (has an active local request) per cycle. This is the
            // one genuinely multi-cache assumption in this model: it's
            // what makes "snoop this cycle" well-defined (a single
            // originator) instead of needing an arbiter/ordering model.
            assume($onehot0(requesting));
        end
    end

    // ================= Safety properties =================
    // Note on labels: a static assert/cover label repeated across generate-
    // for iterations collides in this yosys build (confirmed empirically:
    // a two-line isolated probe, `foo: assert(...)` inside `generate for`,
    // fails with "Cannot add procedural assertion ... because a cell with
    // the same name was already created" on the second iteration -- label
    // text is used as a flat cell name, not scoped per generate instance).
    // So per-cache (per-`gp`) checks below are deliberately left unlabeled;
    // sby still reports exactly which cache instance failed via the
    // hierarchical path (G_SAFETY[<gp>].$check...), it just won't have a
    // short mnemonic name. Only checks that instantiate once (not inside a
    // `for` loop) keep a static label.
    genvar gp;
    generate
        for (gp = 0; gp < N; gp = gp + 1) begin : G_SAFETY
            always @(posedge clk) begin
                if (rst_n) begin
                    // P1: state encoding is always one of the 4 defined
                    // values (trivially true for an unconstrained 2-bit
                    // reg, kept for documentation/consistency with
                    // mesi_line.v's own valid_encoding check).
                    assert(state[gp] <= ST_M);

                    // P2: if this cache holds Exclusive or Modified (has
                    // write permission), no OTHER cache holds ANY copy of
                    // the line. This is the core MESI single-writer
                    // invariant -- it subsumes mutual exclusion of M (two
                    // caches can't both be M, since each would violate the
                    // other's P2) AND forbids the classic coherence bug of
                    // "one cache Modified while another is stale-Shared."
                    assert(!(state[gp] == ST_E || state[gp] == ST_M)
                           || !other_has_line[gp]);
                end
            end
        end
    endgenerate

    // P3: mutual exclusion of Modified, stated directly (not just implied
    // by P2) -- this is the exact headline property this project sets out
    // to prove, so it gets its own explicit, independently-readable
    // assertion rather than relying on P2 alone to cover it.
    wire [N-1:0] m_mask;
    generate
        for (gp = 0; gp < N; gp = gp + 1) begin : G_MMASK
            assign m_mask[gp] = (state[gp] == ST_M);
        end
    endgenerate
    always @(posedge clk) begin
        if (rst_n) begin
            mutex_modified: assert($onehot0(m_mask));
        end
    end

    // ================= Functional correctness (per-cache) =================
    // These are genuine next-cycle claims ("if X held last cycle, Y must
    // hold now"), which is normally what concurrent SVA (`assert property
    // (@(posedge clk) ... |=> ...)`) is for. Tried that first here -- it is
    // valid IEEE 1800-2023 SS16.14 syntax -- and confirmed empirically
    // (an isolated one-line probe, independent of this file's complexity)
    // that this project's yosys build (0.65+67) does not accept bare
    // `assert property (@(...) ...)` module items at all: a plain
    // `my_check: assert property (@(posedge clk) disable iff (!rst_n) a
    // |=> b);` in a trivial 5-line module fails the same "unexpected '@'"
    // parse error this file did. Not a labeling or generate-block issue --
    // the frontend simply doesn't implement that grammar production. Falls
    // back to this project's already-established, empirically-confirmed
    // idiom instead (see docs/systemverilog_ieee1800_rules.md SS4): an
    // immediate assertion inside `always @(posedge clk)`, using $past() to
    // reach back to the antecedent cycle -- same next-cycle claim, same
    // sampled-value safety ($past is evaluated once per clock like a
    // concurrent assertion's sampling would be), different syntax.
    generate
        for (gp = 0; gp < N; gp = gp + 1) begin : G_FUNC
            wire p4_ante = (state[gp] == ST_I) && cpu_read[gp] && !cpu_write[gp] && !other_has_line[gp];
            wire p5_ante = (state[gp] == ST_I) && cpu_read[gp] && !cpu_write[gp] && other_has_line[gp];
            wire p6_ante = cpu_write[gp] && !cpu_read[gp];
            wire p8_ante = (state[gp] == ST_E || state[gp] == ST_M) && snoop_read_in[gp] && !snoop_readex_in[gp];

            always @(posedge clk) begin
                if (rst_n && $past(rst_n)) begin
                    // P4: a read miss with no other owner becomes Exclusive.
                    assert (!$past(p4_ante) || state[gp] == ST_E);

                    // P5: a read miss WITH another current owner becomes
                    // Shared, never Exclusive -- the property that actually
                    // distinguishes this multi-cache model from
                    // mesi_line.v's single-cache one.
                    assert (!$past(p5_ante) || state[gp] == ST_S);

                    // P6: any local write (from any state) results in Modified.
                    assert (!$past(p6_ante) || state[gp] == ST_M);

                    // P7: a remote BusRdX (another cache writing) invalidates
                    // this cache unconditionally -- the direct formal
                    // statement of "no stale data survives an invalidation."
                    assert (!$past(snoop_readex_in[gp]) || state[gp] == ST_I);

                    // P8: a remote BusRd downgrades E/M to Shared, not
                    // Invalid -- the data survives (this cache keeps a
                    // valid, now-shared copy), only the write permission is
                    // revoked.
                    assert (!$past(p8_ante) || state[gp] == ST_S);
                end

                // P9: a write to an already-Exclusive line upgrades to
                // Modified WITHOUT issuing a bus transaction THIS cycle --
                // the "silent upgrade" optimization real MESI
                // implementations rely on for write performance; worth its
                // own property since it's easy to accidentally broadcast on
                // every write and only notice the extra bus traffic outside
                // formal verification. Same-cycle, so no $past() needed.
                if (rst_n) begin
                    assert (!(state[gp] == ST_E && cpu_write[gp]) || !issue_busrdx[gp]);
                end
            end
        end
    endgenerate

    // ================= Coverage =================
    generate
        for (gp = 0; gp < N; gp = gp + 1) begin : G_COVER
            always @(posedge clk) begin
                if (rst_n) begin
                    cover(state[gp] == ST_S);
                    cover(state[gp] == ST_E);
                    cover(state[gp] == ST_M);
                end
                if (rst_n && $past(rst_n)) begin
                    // C4: the silent E->M upgrade path is actually
                    // reachable, not just vacuously true because it never
                    // fires.
                    cover($past(state[gp]) == ST_E && state[gp] == ST_M);
                    // C5: a Modified line gets invalidated by a remote
                    // write -- the transition P7 constrains is genuinely
                    // exercised.
                    cover($past(state[gp]) == ST_M && state[gp] == ST_I);
                end
            end
        end
    endgenerate

    // C6/C7: multi-cache sharing and ownership transfer actually happen,
    // not just each cache's own single-instance state space.
    generate
        if (N >= 2) begin : G_COVER_MULTI
            always @(posedge clk) begin
                if (rst_n) begin
                    reach_two_shared: cover(state[0] == ST_S && state[1] == ST_S);
                end
                if (rst_n && $past(rst_n)) begin
                    // Ownership transfer: cache 0 was Modified, then cache
                    // 1's read pulls both down to Shared in the same
                    // transaction.
                    cover_ownership_transfer: cover($past(state[0]) == ST_M && state[0] == ST_S && state[1] == ST_S);
                end
            end
        end
    endgenerate
    generate
        if (N >= 3) begin : G_COVER_TRIPLE
            always @(posedge clk) begin
                if (rst_n) begin
                    reach_three_shared: cover(state[0] == ST_S && state[1] == ST_S && state[2] == ST_S);
                end
            end
        end
    endgenerate
`endif
endmodule
