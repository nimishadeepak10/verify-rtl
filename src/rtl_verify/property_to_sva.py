"""Plain-English classified property -> candidate SVA boolean expression.

Deliberately the simpler half of Phase 2 — per direct feedback from formal
engineers, converting an already-classified plain-English property into
SVA is comparatively commodity compared to proposing and classifying it
in the first place (property_suggester.py). "Easy to generate" is not
"trustworthy without checking" though: every candidate this returns still
gets run through the real formal backend before being shown as a result —
this module never claims a property is correct, only that it compiles.

The expression style is deliberately narrow — a plain boolean expression,
no concurrent-assertion syntax (`@(posedge clk)`, `|->`) — matching
exactly what backends/symbiyosys.py and formal_props.py were confirmed to
support by actually running them (see formal_props.py's docstring).

Known limitation, found by actually running a generated property, not
theorized: a same-cycle property checked exactly on the edge an
asynchronous reset transitions (e.g. "whenever rst_n is low, light is
S_RED") can be falsified by a genuine race between the DUT's own
`always @(posedge clk or negedge rst_n)` block and this tool's checker
block — both trigger on the same event, and which one's view of the
registered signal "wins" for that one edge is a real, known formal-
verification subtlety, not a bug in the RTL or in this pipeline. If a
property comes back falsified with a counterexample that looks like it's
firing exactly at a reset transition, treat that as a case needing manual
review of the trace rather than an automatic sign the RTL is wrong.
"""

from __future__ import annotations

from . import llm_client
from .analyzer import RtlModule

CONVERT_SCHEMA = {
    "type": "object",
    "properties": {
        "expressible": {"type": "boolean"},
        "expr": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["expressible", "expr", "note"],
    "additionalProperties": False,
}

_SYSTEM = """You convert a single, already-classified plain-English hardware property into a \
boolean SystemVerilog expression usable inside a plain `assert(...)`, `assume(...)`, or \
`cover(...)` statement — NOT a full concurrent `assert property` block. Just the boolean \
expression itself.

Supported syntax (confirmed working by actually running it against a real formal solver):
- Plain boolean/arithmetic/bitwise operators: && || ! == != <= >= < > + - and bit operators.
- Reading the DUT's own port names, given below, exactly as spelled — never invent a signal.
- For "if A then B" / implication, write it as `!A || B` — NOT as `A -> B` or `A |-> B` (both
  confirmed broken: yosys's frontend rejects bare `->` too, not just `|->` — "syntax error,
  unexpected '>'" — even though `->` is valid standard SystemVerilog elsewhere).
- `$past(EXPR)` — the value EXPR had one clock cycle ago. Use this for a "previous cycle" /
  "next cycle" claim (e.g. "X must equal one more than X was last cycle" is
  `X == $past(X) + 1`). The wrapper automatically guards every $past()-using property so it's
  only checked once a genuinely valid previous cycle exists (never the very first evaluated
  cycle, and never immediately after a reset pulse) — write the property's actual logical claim
  and nothing else; do not add your own reset/first-cycle guard, the tool already does this for
  you. Confirmed correct with an unbounded (PDR) proof on an isolated probe, and independently
  confirmed via VCD trace inspection on several real hand-written properties (a cache-line
  fill-then-read check, an ownership-transfer check in a multi-cache coherence model) before
  being enabled here — not assumed safe. **Keep the condition and the comparison at the SAME time
  offset from each other, consistently.** A "condition this cycle implies a change by next cycle"
  claim must be checked as `!$past(CONDITION) || (VALUE == f($past(VALUE)))` — the condition
  sampled from LAST cycle, compared against THIS cycle's value versus LAST cycle's value — not
  `!CONDITION || (VALUE == f($past(VALUE)))` with the condition read from THIS cycle. That second,
  wrong form was a real, confirmed mistake this feature caught in its own end-to-end test (three
  otherwise-reasonable FIFO properties — "no read when empty implies count unchanged", "no write
  when full implies count unchanged", "simultaneous read+write nets to zero change" — all written
  with a current-cycle condition against `$past(count)`, and all three were correctly FALSIFIED by
  the solver with a real counterexample, because they were checking the wrong pair of cycles, not
  because the underlying design or the invariant itself was wrong). When in doubt, match the
  working pattern already confirmed twice: sample the condition with `$past(...)`, compare the
  quantity's current value against `$past(quantity)`.

NOT supported — do not use, even though it looks like it should work (each tested directly
against the real solver, not assumed):
- `A -> B` or `A |-> B` or `A |=> B` — confirmed broken, yosys's parser rejects the arrow syntax
  entirely in this context. Use `!A || B` for same-cycle implication, or `!$past(A) || B` for a
  "if A held last cycle, B must hold now" claim (logically identical to `A |=> B`, and confirmed
  working through this tool's $past() support).
- `@(posedge ...)` clocking events, `##` delay operators, or any other full concurrent-assertion /
  sequence syntax (`assert property (...)`) — confirmed broken: this yosys build's frontend
  rejects the entire `assert property (@(...) ...)` grammar production outright, not just specific
  operators within it (tested directly, including on a newer yosys/oss-cad-suite release — same
  result). `$past()` inside a plain `assert(...)` is the supported substitute.
- A claim spanning more than one cycle back/forward (e.g. "eventually", "within N cycles",
  "always after this point") — `$past()` reaches exactly one cycle back; nesting it arbitrarily
  deep or expressing unbounded/liveness claims is not something this tool has validated and
  should still be declined.
- A signed shift or other signed value-producing sub-expression (`$signed(x) >>> n`,
  `$signed(x) <<< n`) as one of the two RESULT branches of a ternary (`cond ? A : B`) when the
  OTHER branch is unsigned. Per IEEE 1800-2023 §11.8.1 ("If any operand is unsigned, the result is
  unsigned, regardless of the operator"), the whole ternary — including the branch you cast as
  signed — silently collapses to an unsigned (logical) result. Confirmed by reproduction: a module
  where a port is LITERALLY defined as `$signed(a) >>> n`, asserting that port equals
  `$signed(a) >>> n` again (a trivially-true self-check), still fails under this pattern. If a
  property needs a signed comparison as a 0/1 result, `($signed(x) < $signed(y)) ? 1 : 0` is safe
  — comparison results are always 1-bit unsigned per the same clause, regardless of what's
  compared, so no signed value ever reaches the ternary's branches. If a property genuinely needs
  an arithmetic-shift VALUE (not just a comparison), express it without `$signed()`/`>>>` at all:
  `(x >> n) | (x[msb] ? ~(all_ones >> n) : 0)` — logical shift, then OR in a sign-extension mask
  only when the sign bit was set. Prefer declining a property like this if the safe rewrite isn't
  a clean single expression rather than risking the silent-degrade pattern above.

A DUT port named `__dbg_<signal>` is a debug port: read-only visibility into an internal register
that has no port of its own otherwise. Use it exactly like any other port. If it's paired with a
port `__dbgsel_<signal>`, that internal signal is an array, and `__dbgsel_<signal>` is an index
input selecting which element `__dbg_<signal>` reads — left as a free signal in the expression
(not pinned to a literal), this is a genuine "for every array entry" check, which is normal and
fine for a same-cycle property.

If the property is about a single, same-cycle relationship between signals (most safety
properties, range checks, one-hot checks, mutual exclusion, causality between two signals whose
truth is decided in the same cycle), set "expressible": true and give the boolean expression.

If the property is a "previous cycle" / "next cycle" claim reaching back exactly ONE clock edge
(e.g. "X becomes Y one cycle after condition C" is `!$past(C) || (X == Y)`; "X only ever changes
by at most 1 per cycle" is `(X == $past(X)) || (X == $past(X)+1) || (X == $past(X)-1)`), set
"expressible": true and use `$past()` — see the supported-syntax section above. Do not write your
own reset/first-cycle guard; the wrapper adds it automatically.

If the property genuinely requires referencing MORE than one cycle back/forward, or is an
unbounded/liveness claim (words like "eventually", "within N cycles", "always from this point
on"), set "expressible": false, leave "expr" as an empty string, and explain why in "note". Do NOT
invent a same-cycle approximation for a genuinely multi-cycle claim, and do not chain `$past()`
calls speculatively for claims spanning more than one cycle — a wrong same-cycle or wrongly-nested
check produces a misleading result, which is worse than declining to convert it at all.
"""


def build_user_prompt(module: RtlModule, kind: str, description: str, rationale: str) -> str:
    ports = ", ".join(p.name for p in module.ports)
    return (
        f"DUT ports: {ports}\n"
        f"Property kind: {kind}\n"
        f"Property (plain English): {description}\n"
        f"Rationale: {rationale}\n"
    )


def convert_to_sva(module: RtlModule, kind: str, description: str, rationale: str = "") -> dict:
    """Return {"expressible": bool, "expr": str, "note": str}.

    When expressible is False, expr is empty — the caller must not run it
    through the formal backend; there's nothing there that means what the
    English property claimed.
    """
    user = build_user_prompt(module, kind, description, rationale)
    result = llm_client.complete_structured(_SYSTEM, user, CONVERT_SCHEMA, max_tokens=500)
    result["expr"] = result.get("expr", "").strip()
    return result


def build_retry_prompt(
    module: RtlModule, kind: str, description: str, rationale: str,
    previous_expr: str, error_log: str,
) -> str:
    base = build_user_prompt(module, kind, description, rationale)
    return (
        f"{base}\n"
        f"A previous attempt to express this property failed to compile against the real "
        f"formal solver:\n"
        f"Previous expression: {previous_expr}\n"
        f"Tool error (truncated to the last ~1500 chars):\n{error_log[-1500:]}\n\n"
        f"Fix the expression given this concrete error, still respecting the supported/"
        f"unsupported syntax rules above. Common causes: a signal name that doesn't exactly "
        f"match the DUT's port list, a width mismatch, or invalid operator usage. If you now "
        f"believe this property genuinely cannot be expressed safely (not just a fixable typo), "
        f"set expressible=false instead of guessing again."
    )


def convert_to_sva_retry(
    module: RtlModule, kind: str, description: str, rationale: str,
    previous_expr: str, error_log: str,
) -> dict:
    """One automatic fix attempt after a genuine tool/compile error (not a
    falsification — that's a legitimate result, never retried). Feeds the
    concrete solver error back to the model rather than guessing blind.
    """
    user = build_retry_prompt(module, kind, description, rationale, previous_expr, error_log)
    result = llm_client.complete_structured(_SYSTEM, user, CONVERT_SCHEMA, max_tokens=500)
    result["expr"] = result.get("expr", "").strip()
    return result
