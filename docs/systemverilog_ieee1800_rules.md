# SystemVerilog (IEEE 1800-2023) rules VerifyRTL's code depends on

**What this is:** a small, growing set of normative rules from the actual IEEE Std 1800-2023
SystemVerilog LRM (the licensed standard, not a summary or tutorial), each cited by clause
number, that either explain a real bug this project already found the hard way or ground a
syntax choice the code makes. This is **not** a copy of the standard — IEEE 1800-2023 is
copyrighted and licensed per-seat via IEEE Xplore; nothing here reproduces its text beyond a
short quoted phrase for precision, the way a paper cites a source. Every entry is written in this
project's own words, with the clause number so a maintainer with access to the standard can go
verify it directly.

**Why this file exists:** several stages of the complexity-validation roadmap
(`scripts/test_*.py`, see the README) hit real, subtle SystemVerilog semantics empirically —
through a failing formal proof, not through reading the spec first. That's consistent with this
project's whole practice of verifying against real tool behavior rather than assuming. But once a
rule is found the hard way, it should be captured with its authoritative citation so it doesn't
need re-discovering, and so future code (this project's own generation logic, or an LLM prompt)
can be told the rule directly instead of relying on trial and error again.

**Maintenance:** add an entry here whenever a real bug or design decision turns out to hinge on a
specific IEEE 1800 clause — this file is expected to grow stage by stage, not be written once.

---

## 1. Conditional operator (`?:`) signedness — §11.4.11 and §11.8.1

**The rule:** for the conditional operator's two result branches (`cond ? A : B`), when both `A`
and `B` are integral types, §11.8.1 ("Rules for expression types") states plainly: *"If any
operand is unsigned, the result is unsigned, regardless of the operator."* This means if `A` is
built from a signed cast or a signed shift (`$signed(x) >>> n`) but `B` is a plain unsigned
expression, the **entire conditional expression** — including the branch that was explicitly cast
signed — is treated as unsigned. The cast doesn't protect its own branch; the ternary's
signedness is decided by looking at both branches together (§11.6.1's self-determined vs.
context-determined distinction: neither branch of a non-self-determined `?:` is independently
signed once paired against an unsigned sibling).

**What this actually breaks:** an arithmetic right shift written as
`cond ? ($signed(x) >>> n) : (x >> n)` silently computes a **logical** shift for the signed branch
too — the `$signed()` cast and `>>>` operator are textually present but functionally inert.

**How this project found it:** empirically, twice, independently, via two different SystemVerilog
operators, in two different tools:
- First via the **ternary operator** in `examples/rv32i_core.v`'s SRA/SRAI ALU logic
  (`cond ? ($signed(x) >>> n) : (x >> n)`), running under real Icarus Verilog simulation — a
  hand-assembled test program's `SRAI` instruction produced a logical-shift result instead of the
  correct arithmetic one.
- Again via the **equality operator**, in `scripts/test_rv32i_rvfi_checks.py`'s own hand-written
  formal property for the same instructions, running under real SymbiYosys/yosys formal
  verification. Restructuring the check away from a ternary entirely (two separate implications
  instead) did *not* fix it, which at the time looked like it disproved the ternary-signedness
  hypothesis — it didn't; it's the same §11.8.1 rule reached through a different route. The final
  failing form was a plain `rd_wdata == ($signed(rs1_rdata) >>> shamt)`, where `rd_wdata` is an
  unsigned port. §11.8.2 carves out an explicit exception for exactly this operator: *"The
  relational and equality operators have operands that are neither fully self-determined nor fully
  context-determined. The operands shall affect each other as if they were context-determined
  operands with a result type and size... determined from them."* Combined with §11.8.1, that
  shared type is unsigned the moment either side is — so `rd_wdata`'s unsignedness propagates
  *into* the signed shift on the other side of `==`, same degrade as the ternary case, just via
  the equality operator's own operand-matching rule instead of the conditional operator's.
  Confirmed two ways: (1) a minimal, trivially-true isolated self-check — a module where a port
  `r` is *literally* defined as `$signed(a) >>> n`, asserting `r == ($signed(a) >>> n)` — still
  failed while `r` was an unsigned port; and (2) re-running the identical self-check with `r`
  declared **signed** instead — predicted by the clause to fix it, and it did, PASS. That's about
  as directly as an IEEE clause gets to be confirmed against real tool behavior.

**The fix, both times:** don't let a signed value-producing sub-expression meet an unsigned
sibling through *any* operator that mixes operand types to determine a shared evaluation type —
ternary branches and equality/relational operands both qualify, and there may be others not yet
hit in practice. Two safe alternatives, both confirmed working:
- Make both sides genuinely the same signedness — plain `if`/`else` instead of `?:` sidesteps the
  ternary version entirely (§11.4.11's conditional-operator rule doesn't apply to `if`/`else`);
  declaring the comparison target itself `signed` sidesteps the equality version.
- For arithmetic shift specifically, avoid `$signed()`/`>>>` entirely: logical-shift the value,
  then OR in a sign-extension mask exactly when the sign bit was set —
  `(x >> n) | (x[msb] ? ~(all_ones >> n) : 0)`. This is what
  `scripts/test_rv32i_rvfi_checks.py`'s `arith_shift_expr()` does, and what `examples/rv32i_core.v`
  itself does after the fix — the most robust option since it never needs `$signed()` to interact
  with anything else in the surrounding expression at all.

**Where this rule is now enforced going forward:** `property_to_sva.py`'s LLM conversion prompt
explicitly tells the model not to generate this pattern, with the same citation and the same safe
rewrite, so future SVA conversions don't have to rediscover this the hard way.

**A related, narrower-scope fact from the same clause, confirmed safe:** comparison and reduction
operator results are unsigned *regardless of their operands* (§11.8.1: "Comparison and reduction
operator results are unsigned, regardless of the operands"). So
`($signed(x) < $signed(y)) ? 32'd1 : 32'd0` is **not** the bug above — the signed comparison
collapses to a 1-bit unsigned result before it ever reaches the ternary, so there's no signed
value in either branch to degrade. `scripts/test_rv32i_rvfi_checks.py`'s SLT/SLTI checks use
exactly this pattern and proved correctly.

## 2. Self-determined vs. context-determined expressions — §11.6.1, §11.8.1, §11.8.2

**The rule:** every subexpression is either **self-determined** (its width/sign depend only on
itself — e.g. an operand of `>>`/`<<`/`>>>`/`<<<` other than the value being shifted, per Table
11-21) or **context-determined** (its width/sign are decided by the surrounding expression and
propagated back down, per §11.8.2's three-step evaluation: determine size, determine sign, then
propagate the result's type back down to every context-determined operand). Bit-select, part-
select, concatenation, and comparison/reduction results are **always unsigned regardless of their
operands** (§11.8.1) — casting the operand doesn't make the *result* signed.

**Why this matters for VerifyRTL:** every property this project generates or hand-writes is a
single boolean/arithmetic expression built by string concatenation (`generate_formal_wrapper()` in
`formal_props.py`, and the `rd_check()`/`branch_pc_check()`/etc. helpers in
`scripts/test_rv32i_rvfi_checks.py`). There's no compiler front-end double-checking these before
they reach the solver — a signedness mistake ships straight through to a real (and sometimes
misleading) formal verdict. Rule 1 above is the concrete instance that actually bit this project;
this entry is the general principle it's an instance of, for the next time a similar pattern comes
up (shifts are the sharpest edge here, but any operator mixing a self-determined signed
subexpression into a wider unsigned context is a candidate).

## 3. Immediate assertion grammar — §16.3

**The rule:** a simple immediate assertion is `assert ( expression ) action_block`, where
`action_block` can be empty (just a trailing `;` — grammatically `action_block ::= statement_or_
null`, and `statement_or_null` may be null). An optional `identifier :` label is permitted before
the statement (§16.3's own worked examples show `assert_f: assert(f) ...;`), creating a named
scope usable in `%m` and in tool diagnostics. The expression itself is interpreted exactly like a
procedural `if` condition: `x`/`z`/`0` is false, anything else is true (§16.3, §16.6).

**Why this matters for VerifyRTL:** `generate_formal_wrapper()` emits exactly this form —
`name: assert (expr);` (or `assume`/`cover`) with no action block — inside a clocked or
combinational `always` block. Confirmed here against the actual grammar: this is a syntactically
valid **simple immediate assertion**, not an ad hoc or nonstandard construct.

## 4. Immediate vs. concurrent assertions — §16.3 vs. §16.5, §16.14

**The rule:** these are two genuinely different constructs, not two spellings of the same thing.
An **immediate** assertion (`assert (expr);`) is a procedural statement, evaluated using the
*current* value of its operands at the point program control reaches it — same as an `if`.
A **concurrent** assertion (`assert property (@(posedge clk) expr);`, keyword `property` present)
is clock-tick-based and evaluates *sampled* values — by default the value from the *Preponed*
region of the current time step (§16.5.1) — and is itself evaluated in the *Observed* region
(§16.5). This sampled-value semantics is specifically what makes concurrent assertions immune to
the same-time-step race a plain procedural read of a register can otherwise have against another
`always @(posedge clk)` block updating that same register via a nonblocking assignment in the same
edge.

**Why VerifyRTL deliberately uses immediate, not concurrent, assertions:** `generate_formal_wrapper()`
generates the immediate form (§16.3), not `assert property` (§16.14) — checked directly against
riscv-formal's own official checker (`checks/rvfi_insn_check.sv`, fetched and read from
github.com/YosysHQ/riscv-formal while building Stage 7), which uses the identical
immediate-assertion-inside-`always @*` pattern, not `assert property`. For SymbiYosys/yosys's
formal flow specifically — bounded/unbounded model checking on a synthesized netlist, not
event-driven simulation — this is the established, working idiom, and this project's own extensive
cross-validation against real simulation (every stage in the README's stress-testing table) hasn't
surfaced the race-condition class of problem the sampled-value mechanism exists to prevent for a
*simulator*.

**When this would need to be revisited:** if this project ever adds a **simulation-based**
assertion checker (as opposed to the formal-only backend it has today), the immediate/concurrent
distinction stops being a formality — a real event-driven simulator genuinely can race a procedural
`assert` against another block's nonblocking-assignment update in the same time step, and switching
to real `assert property (@(posedge clk) ...)` syntax would be the correct fix at that point, not
just a style preference.
