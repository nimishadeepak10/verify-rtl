# Formal Property Reference — curated grounding for VerifyRTL

**What this is:** a hand-curated set of vetted, sourced material — published research, official
standards, and actively-maintained open-source formal-verification projects — used to ground
VerifyRTL's LLM-assisted property suggestions (Phase 2) in real, checkable patterns instead of
the model's unaudited pretrained recall. This is **not** model fine-tuning; Claude cannot be
fine-tuned by an API customer. This is retrieval-grounding: relevant sections of this file get
included in the property-suggestion and SVA-conversion prompts, the same way a human engineer
would keep a reference book open while writing properties.

**Sourcing rule:** every entry below is either a peer-reviewed/arxiv paper, an official industry
standard (Accellera), a real actively-maintained open-source project, or a named, individually
attributable practitioner with a durable track record in this exact space (not an anonymous forum
post, not an undated tutorial). Each entry cites where it came from.

**Maintenance:** static and human-curated, refreshed periodically — not re-searched live on every
property-generation call. See the "How this file gets used" section for why.

**Last curated:** 2026-07-26

---

## 1. Canonical property shapes (Dwyer, Avrunin & Corbett, 1999)

The most-cited taxonomy in finite-state verification for *what kind of thing* a property is
allowed to claim. Two families:

**Occurrence patterns** — claims about whether something happens at all:
- **Absence** — a given state/event never occurs.
- **Existence** — a given state/event must occur at least once.
- **Bounded Existence** — a given state/event occurs at most (or exactly) *k* times.
- **Universality** — a given state/event holds throughout execution.

**Order patterns** — claims about the relative order of events:
- **Precedence** — event *B* may only occur if event *A* occurred first ("*A* precedes *B*").
- **Response** — if event *A* occurs, event *B* must eventually follow ("*A* leads to *B*").
- **Chain Precedence / Chain Response** — the same two shapes, generalized to a sequence of
  events rather than a single pair.

**Why this matters for VerifyRTL:** a plain-English property proposal that doesn't map onto one
of these named shapes is usually either compound (hides multiple claims — a completeness risk we
already flagged) or too vague to compile into an unambiguous assertion. Phase 2's plain-English
review step should tag each proposed property with which pattern it is, not just accept free-form
prose.

Source: Dwyer, Avrunin & Corbett, *"Patterns in Property Specifications for Finite-State
Verification,"* ICSE 1999 — [dl.acm.org/doi/10.1145/302405.302672](https://dl.acm.org/doi/10.1145/302405.302672);
pattern catalog maintained at [matthewbdwyer.github.io/psp](https://matthewbdwyer.github.io/psp/patterns.html).

---

## 2. Two rules that make or break a real formal property

From Dan Gisselquist's zipcpu.com — one of the most-cited, still-actively-maintained practical
formal-verification references in the open-source Yosys/SymbiYosys community (the same toolchain
VerifyRTL uses).

**Rule 1 — assume the inputs, assert the internals and outputs.** For a module under test,
constrain *inputs* with `assume` (what the environment guarantees) and check *internal state and
outputs* with `assert` (what the design itself must guarantee). Getting this backwards — asserting
on an input — silently turns a check into a constraint that can hide real bugs.

**Rule 2 — every `assert` needs a matching `cover`.** Gisselquist explicitly describes adding
`cover()` properties only *after* being burned by proofs that passed vacuously — the assumptions
were strong enough to make the interesting scenario unreachable, so the assert never actually got
exercised. This is the exact same class of bug VerifyRTL hit independently in Phase 1 (the
unconstrained-reset-state false counterexample) — different failure mode, same root cause: a
proof can look green for the wrong reason. Phase 2's planned vacuity check (pair every `assert`
with a `cover` proving its trigger condition is reachable) is directly validated by this.

**Caution — the assume/assert "swap" technique for aggregating verified modules is powerful but
can produce false positives.** Gisselquist explicitly warns his own swap-based aggregation
approach "can leave you believing your design works when it does not." Treat any property derived
by swapping a module's asserts into another module's assumes with extra scrutiny — re-run it,
don't just trust the derivation.

Sources: [zipcpu.com/formal/2018/12/28/axilite.html](https://zipcpu.com/formal/2018/12/28/axilite.html),
[zipcpu.com/formal/2018/04/23/invariant.html](https://zipcpu.com/formal/2018/04/23/invariant.html),
index at [zipcpu.com/formal/formal.html](https://zipcpu.com/formal/formal.html).

---

## 3. Standard reusable checker vocabulary (Accellera OVL)

The Open Verification Library is Accellera's standardized, industry-wide catalog of assertion
checkers for simulation *and* formal use, covering Verilog, SystemVerilog, VHDL, and PSL. Rather
than inventing property shapes ad hoc, VerifyRTL's rule-based property templates should reuse
OVL's naming and semantics wherever a design matches one of these standard patterns:

- **One-hot / one-cold** — exactly one (or zero) bit of a bus is set — the standard shape for
  arbiter grant signals.
- **Handshake / req-ack** — a request signal must eventually be met by an acknowledge, with
  defined stability rules while pending.
- **FIFO** — no overflow, no underflow, no data reordering across a queue.
- **Mutex** — at most one of a set of signals is asserted at a time.
- **Window** — a signal must assert within a bounded window following a trigger.
- **Never-unknown** — a signal must never carry an X/Z value where a defined value is required.

This maps directly onto the naming-based signal-role detection already built in
`generators/comb_assert_tb.py` (its `req`/`grant`/`grant_valid`/`weight_in`/`weight_out` heuristics)
— that file already identifies exactly the signal roles OVL's checkers are designed for; it just
emits simulation-time checks today rather than OVL-vocabulary formal properties.

Sources: [accellera.org/downloads/standards/ovl](https://www.accellera.org/downloads/standards/ovl);
OVL 2.6 Language Reference Manual (PDF, hosted by Intel Community) —
[community.intel.com …/ovl_lrm.pdf](https://community.intel.com/cipcp26785/attachments/cipcp26785/programmable-devices/57676/1/ovl_lrm.pdf).

---

## 4. Real-world worked patterns from maintained open-source formal projects

**riscv-formal (YosysHQ)** — the formal verification framework YosysHQ itself uses to verify
RISC-V CPU cores with SymbiYosys. Two load-bearing, independently-confirmed facts:

- *"All properties are expressed using immediate assertions/assumptions for maximal
  compatibility with other tools."* This matches, byte for byte, the fix VerifyRTL's own
  `formal_props.py` landed on after discovering that concurrent `assert property (@(posedge clk)
  …)` syntax fails on yosys's built-in Verilog frontend — an independent real-world project
  reaching the identical conclusion.
- Verification is done via a **wrapper module that instantiates the core under test** (with RVFI
  helper macros for the wrapper's own port wiring) — the same wrapper-instantiation shape
  `formal_props.py` uses instead of `bind` (which yosys silently drops).

**ZipCPU's AXI-lite property checklist** — a concrete, reusable checklist for any handshake-style
protocol (directly applicable to req/grant/valid designs like `examples/priority_weighted_arbiter.sv`):
1. Reset initialization — all `*VALID` outputs deasserted immediately after reset.
2. Data/request stability — a pending (valid-but-not-ready) transaction's data must not change.
3. Transaction counting — outstanding requests must match completions one-to-one.
4. Combinational independence — a `valid` signal must never combinationally depend on the
   matching `ready` (prevents combinational loops).
5. Liveness coverage — a `cover` proving a full successful transaction is actually reachable, not
   just the safety asserts.

Sources: [github.com/YosysHQ/riscv-formal](https://github.com/YosysHQ/riscv-formal);
[zipcpu.com/formal/2018/12/28/axilite.html](https://zipcpu.com/formal/2018/12/28/axilite.html).

---

## 5. Peer-reviewed research on LLM-assisted property generation (2025–2026)

Each of these directly validates or informs a specific design choice already made or planned in
VerifyRTL's roadmap — noted per entry.

- **ProofLoop** ("From Language to Logic: Bridging LLMs & Formal Representations for RTL Assertion
  Generation") — a tool-augmented ReAct agent that generates SVA from natural language using a
  **solver-in-the-loop** approach, refining against JasperGold proof feedback. *Validates:* Phase
  2's planned retry loop (feed the SymbiYosys compile error or counterexample back to the LLM,
  rather than accepting one-shot output).
  [arxiv.org/html/2604.23100](https://arxiv.org/html/2604.23100)

- **SpecAlign** — a specification-centric framework that evaluates and refines the *semantic
  alignment* of LLM-generated SVA against the spec, without needing golden RTL, via property- and
  SVA-alignment refinement loops. *Validates:* the emphasis on vacuity/meaningfulness over
  syntactic validity alone — a property that compiles and passes isn't automatically a property
  that means what the spec said.
  [arxiv.org/pdf/2605.25181](https://arxiv.org/pdf/2605.25181)

- **SANGAM** — uses LLM-guided Monte Carlo Tree Search to iteratively refine SVA against critic
  and formal-tool feedback from multi-modal specifications. *Validates:* iterative solver-checked
  refinement is the field's actual state of the art, not single-shot generation — reinforces
  ProofLoop's pattern from a second, independent group.
  [arxiv.org/pdf/2506.13983](https://arxiv.org/pdf/2506.13983)

- **AssertionForge** — builds a knowledge graph fusing information extracted from both the spec
  *and* the RTL, then generates assertions grounded in that structured graph rather than raw text.
  *Validates:* the whole premise of this reference file — grounding generation in curated
  structured knowledge instead of an LLM's raw parametric recall, at a lighter weight (a static
  markdown reference here vs. a full knowledge graph there).
  [arxiv.org/html/2503.19174v2](https://arxiv.org/html/2503.19174v2)

- **STELLAR** — structure-guided retrieval and generation for formal verification assertions.
  *Validates:* retrieval-augmented grounding (pulling in relevant existing patterns before
  generating) measurably improves LLM-generated SVA quality over ungrounded generation.
  [arxiv.org/pdf/2601.19903](https://arxiv.org/pdf/2601.19903)

---

## 6. How this file gets used

This is loaded as context into Phase 2's two LLM calls:

1. **Plain-English property proposal** — the LLM is asked to tag each proposed property against
   the Dwyer/Avrunin/Corbett pattern it matches (§1) and, where the design's signal names match an
   OVL-recognized role (§3), to propose the corresponding standard checker rather than inventing
   an ad hoc equivalent.
2. **SVA conversion** — the LLM is reminded of the assume/assert input/output split (§2 rule 1),
   told to always propose a matching `cover` (§2 rule 2), and shown the immediate-assertion +
   wrapper-instantiation shape already proven to work in `formal_props.py` (§4) so it doesn't
   propose syntax that will fail the same way `bind` and concurrent assertions already did.

Refresh this file periodically (new papers, new lessons from VerifyRTL's own failures) rather than
re-searching live on every call — a static, human-reviewed corpus is cheaper, faster, and doesn't
risk pulling in an unvetted source mid-generation the way a live search could.
