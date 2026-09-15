# Verification Plan — MESI Cache Coherence Protocol (multi-cache formal model)

**Document status:** live — reflects `examples/mesi_multi_cache.v` / `examples/mesi_cache_core.v`
and the `.sby` configs in `MESI_Formal/` as of the commit that adds this file. Regenerate §5/§6 if
a property, assumption, or cover point is added, renamed, or its verdict changes.

**Scope of this plan:** formal verification only, via [SymbiYosys](https://github.com/YosysHQ/sby)
against hand-written SVA (SystemVerilog Assertions), inspected with [GTKWave](https://gtkwave.sourceforge.net/)
when a run produces a trace. There is no simulation testbench for this design — unlike this
project's other DUTs, a snooping cache-coherence protocol's interesting behavior *is* the
cross-instance interaction, which is exactly what formal (exhaustive over reachable states) checks
better than a finite set of directed simulation vectors would.

---

## 1. Design under test

| | |
|---|---|
| **DUT** | `examples/mesi_multi_cache.v` (top), instantiating `examples/mesi_cache_core.v` N times |
| **Protocol** | MESI (Modified / Exclusive / Shared / Invalid) snooping cache coherence |
| **Configuration verified** | N = 3 caches sharing one cache line over one snooping bus |
| **Bus model** | Single shared bus: at most one cache is granted the bus (may issue a local CPU request) per cycle; every other cache observes that cycle's transaction as a snoop |
| **Scope simplification** | One cache line, no tags/addresses/replacement — deliberate: coherence-protocol correctness is a per-line property. A real multi-line, multi-address cache must satisfy exactly these same invariants independently for every line; modeling one line proves the protocol logic without paying for address-space state-space blowup that adds nothing to *this* question |
| **Related, single-cache reference model** | `examples/mesi_line.v` — the original single cache-line FSM (no cross-instance behavior), kept as-is; `mesi_cache_core.v` is its logic extended with the two signals a lone cache has no way to define: whether another cache currently holds the line, and what bus transaction a local request implies for everyone else |

## 2. Verification methodology

**Tool chain:** Yosys (SV frontend) → SymbiYosys → solver (`smtbmc`, `mode prove` uses BMC +
k-induction; `mode cover` for reachability). GTKWave for opening any `.vcd` trace a run produces
(a counterexample, or a cover witness) to inspect the actual signal-level scenario rather than
trusting the solver's verdict on faith.

**Property style — two different assertion forms, deliberately:**
- **Safety invariants** (§5, P1–P3, P10): plain immediate `assert` inside `always @(posedge clk)`
  — same-cycle claims ("this must never be true right now"), no history needed.
- **Functional-correctness / transition properties** (§5, P4–P9): immediate `assert` combined with
  `$past()` inside `always @(posedge clk)` — next-cycle claims ("if X held last cycle, Y must hold
  now"). **Note:** the natural IEEE 1800 tool for this is concurrent SVA
  (`assert property (@(posedge clk) ... |=> ...)`, §16.14) and that was the first thing tried here.
  It was rejected outright by this project's yosys build (0.65+67) — confirmed with an isolated
  5-line probe, independent of this file's complexity, that fails with the identical
  `syntax error, unexpected '@'` this file's first attempt hit. Not a project-specific SVA
  limitation to design around case-by-case; this yosys build's Verilog frontend does not implement
  that grammar production for bare module-item concurrent assertions at all. `$past()`-based
  immediate assertions express the same next-cycle semantics and are what this project has used
  successfully elsewhere (see `docs/systemverilog_ieee1800_rules.md` §4, which already flagged
  exactly this kind of multi-instance/temporal claim as the case worth revisiting if a genuine need
  for concurrent assertions ever arose — it did, and the tool didn't support it, so this plan
  documents the substitute rather than silently working around it).

**A second, unrelated yosys-frontend limitation found and worked around while building this DUT:**
a static `assert`/`cover` label repeated across `generate for` loop iterations collides —
confirmed with another isolated probe — because this frontend uses the label text as a flat cell
name rather than scoping it per generate instance. Per-cache checks inside a `generate for` are
therefore left unlabeled below; SymbiYosys still reports exactly which cache instance failed via
the hierarchical instance path in its counterexample output, it just won't have a short mnemonic
name. Checks that instantiate once (not inside a `for` loop) keep their labels.

**Verdict discipline:** a safety property counts as PROVEN only when `mode prove` returns pass for
both basecase and induction (an unbounded proof, not a bounded depth-20 sample that happened not
to find a bug). A coverage goal counts as reached only when `mode cover` reports the specific
witness for that statement, and — for the properties this plan specifically claims to demonstrate
(ownership transfer) — only after opening the actual `.vcd` and confirming the trace shows what it
claims to, not just trusting the solver's PASS label.

## 3. Environment assumptions

| ID | Assumption | Why it's needed / justified |
|---|---|---|
| A1 | Each cache issues at most one of `cpu_read`/`cpu_write` per cycle | A cache can't be doing two different local operations on the same line in the same cycle — this is a property of what "one request" means, not a restriction that hides real behavior |
| A2 | At most one cache is granted the bus (has an active local request) per cycle (`$onehot0` over all N caches' request activity) | Models a single shared/arbitrated bus, exactly as a real snooping bus enforces via its arbiter. This is the one genuinely multi-cache assumption in this model — it's what makes "this cycle's snoop originator" well-defined without needing to also formally model the arbiter itself (out of scope: arbitration fairness/starvation is a separate concern from coherence correctness, which is what this plan verifies) |

Both are environment assumptions (`assume`), not DUT behavior — they constrain what inputs the
solver is allowed to consider, matching what a real bus/arbiter would actually present to these
caches. Removing A2 and re-running was not done as part of sign-off (it would conflate two
different questions — bus arbitration vs. coherence-protocol correctness — into one model) but is
a natural follow-up if this plan is ever extended to also formally verify the arbiter.

## 4. Properties

**Safety invariants** (checked every cycle, no temporal history):

| ID | Property | Statement |
|---|---|---|
| P1 | `valid_encoding` | Each cache's `state` is always one of {I, S, E, M} |
| P2 | `single_writer` | If a cache holds E or M, no other cache holds any copy of the line (subsumes P3 and forbids the classic "one cache Modified, another stale-Shared" coherence bug) |
| P3 | `mutex_modified` | At most one cache is in M at any time — the headline mutual-exclusion property this plan sets out to prove, stated directly (not just relying on P2) |

**Functional-correctness / transition properties** (next-cycle claims, `$past()`-based):

| ID | Property | Statement |
|---|---|---|
| P4 | `read_miss_exclusive` | A read miss (`I`, `cpu_read`, no other owner) transitions to E |
| P5 | `read_miss_shared` | A read miss WITH another current owner transitions to S, never E — the property that actually distinguishes this multi-cache model from `mesi_line.v`'s single-cache one |
| P6 | `write_becomes_modified` | Any local write, from any starting state, transitions to M |
| P7 | `remote_write_invalidates` | A remote BusRdX (another cache writing) invalidates this cache unconditionally — the direct formal statement of "no stale data survives an invalidation" |
| P8 | `remote_read_downgrades` | A remote BusRd downgrades E/M to S, not I — data survives, only write permission is revoked |
| P9 | `silent_upgrade` | A write to an already-Exclusive line upgrades to M **without** issuing a bus transaction that cycle — the real MESI write-performance optimization; easy to accidentally break by over-broadcasting, and only observable formally (a functional simulation wouldn't necessarily catch the extra unwanted bus traffic as wrong) |

## 5. Traceability matrix

All properties are instantiated once per cache (N=3 → P1, P2, P4–P9 each produce 3 checks;
P3 is a single cross-cache check) — 3×8 + 1 = **25 safety/correctness checks**, all currently ✅.

| Property | Result | Method |
|---|---|---|
| P1 `valid_encoding` ×3 | ✅ PROVEN | k-induction |
| P2 `single_writer` ×3 | ✅ PROVEN | k-induction |
| P3 `mutex_modified` | ✅ PROVEN | k-induction |
| P4 `read_miss_exclusive` ×3 | ✅ PROVEN | k-induction |
| P5 `read_miss_shared` ×3 | ✅ PROVEN | k-induction |
| P6 `write_becomes_modified` ×3 | ✅ PROVEN | k-induction |
| P7 `remote_write_invalidates` ×3 | ✅ PROVEN | k-induction |
| P8 `remote_read_downgrades` ×3 | ✅ PROVEN | k-induction |
| P9 `silent_upgrade` ×3 | ✅ PROVEN | k-induction |

Reproduce: `sby -f MESI_Formal/mesi_multi_cache.sby` from the `MESI_Formal/` directory. Actual
run: `engine_0 (smtbmc) returned pass for basecase` + `... for induction` →
`successful proof by k-induction` → `DONE (PASS, rc=0)`.

## 6. Coverage

| ID | Cover goal | Demonstrates |
|---|---|---|
| C1 | `reach_shared` (×3) | Each cache can individually reach S |
| C2 | `reach_exclusive` (×3) | Each cache can individually reach E |
| C3 | `reach_modified` (×3) | Each cache can individually reach M |
| C4 | `cover_silent_upgrade_reached` (×3) | The E→M silent-upgrade path (P9) is actually exercised, not just vacuously true because it never fires |
| C5 | `cover_modified_then_invalidated` (×3) | A cache that was Modified is later Invalidated (P7's transition is genuinely exercised) |
| C6 | `reach_two_shared` | Two different caches hold S simultaneously — real sharing, not just each cache's own isolated state space |
| C7 | `cover_ownership_transfer` | A cache that was Modified downgrades to S **at the same cycle** another cache's read pulls it to S — a full ownership-transfer transaction |
| C8 | `reach_three_shared` | All three caches hold S simultaneously |

All 8 cover goals (18 individual witnesses across the 3 cache instances + cross-cache points) were
**reached** — `sby -f MESI_Formal/mesi_multi_cache_cover.sby` → `DONE (PASS, rc=0)`, every cover
statement in the summary listed with `reached cover statement ... step N`.

**C7 verified by hand, not just by the solver's label** — opened `trace15.vcd`
(`MESI_Formal/mesi_multi_cache_cover/engine_0/trace15.vcd`) and read the `state[0..2]` signal
changes directly:

| Cycle | `state[0]` | `state[1]` | `state[2]` |
|---|---|---|---|
| reset | I | I | I |
| 1 | **M** | I | I |
| 2 | **S** | **S** | I |

Cache 0 reaches Modified, then in the very next sampled cycle both cache 0 (downgrading) and
cache 1 (a remote read pulling it to Shared) land on S together — exactly the ownership-transfer
scenario C7 claims, confirmed from the raw trace rather than trusted on the solver's say-so.

## 7. Sign-off criteria

This DUT is considered formally signed off when:
1. Every property in §5 is ✅ PROVEN by k-induction (not just BMC-bounded, and not FALSIFIED/ERROR).
2. Every cover goal in §6 is reached, with at least the headline scenario (C7, ownership transfer)
   independently confirmed from its raw trace, not only the solver's summary label.
3. Both environment assumptions (§3) are documented and justified, not silently baked into the
   RTL where a reader can't see what's assumed vs. proven.

As of this document, all three conditions hold.

## 8. Explicit non-goals

This plan proves the MESI *coherence protocol's* state-transition correctness for one line shared
by 3 caches over an idealized single-cycle snooping bus. It does **not** claim:
- Multi-line / multi-address behavior, cache capacity, or replacement policy — out of scope by
  construction (§1); the invariants proved here must independently hold per line in a real cache,
  which this model represents.
- Bus arbitration fairness or starvation-freedom — A2 (§3) assumes a well-behaved arbiter exists;
  this plan doesn't verify the arbiter itself.
- Data-path correctness (actual cache-line contents/values) — this model is state-only (I/S/E/M),
  matching real formal MESI verification practice, where protocol correctness and data-path
  correctness are typically verified as separate concerns.
- Timing/physical implementation, or scaling behavior to N > 3 caches (the properties and their
  proofs are per-N; re-running at a different N is mechanical but wasn't done as part of this
  sign-off — the state space is small enough that N=3 already exercises every qualitatively
  distinct interaction: one cache alone, two sharing, and a three-way share/transfer).
