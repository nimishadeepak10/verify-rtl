"""The Dwyer/Avrunin/Corbett property-specification taxonomy, as a
first-class, reusable data source -- not just prose baked into a prompt.

Source: Dwyer, Avrunin & Corbett, "Patterns in Property Specifications
for Finite-State Verification," ICSE 1999; pattern catalog maintained at
matthewbdwyer.github.io/psp. Already partially adopted in this project
(`property_suggester.py`'s own `PATTERNS` list and its "tag each property
with the closest Dwyer/Avrunin/Corbett pattern" prompt rule, and
`docs/formal_property_reference.md` §1) -- this module adds the piece
that was missing: the taxonomy's SECOND dimension, scope, and a way to
actually USE the resulting (pattern, scope) tags once collected, rather
than letting them sit unread in each property's own JSON entry.

The full taxonomy is two orthogonal dimensions:

  1. PATTERN -- what kind of claim is being made (Absence, Existence,
     Bounded Existence, Universality, Precedence, Response, Chain
     Precedence, Chain Response). `property_suggester.py` already
     captures this.
  2. SCOPE -- WHERE in an execution the pattern applies (Global, Before
     <event>, After <event>, Between <event> and <event>, After <event>
     Until <event>). This was previously uncaptured in this project --
     confirmed by checking the actual schema and prompt before writing
     this: `pattern` was a required field, `scope` didn't exist at all.

Scope matters for hardware specifically, not just as an academic
completeness point: this project's own `$past()`-history-guard work
(`generate_formal_wrapper()` wrapping any `$past()`-using property in
`!$initstate() && <reset not currently or previously asserted>`) is
already, mechanically, always applying something very close to an
"After reset deasserts" scope to every one-cycle-back claim -- without
ever surfacing that as a first-class concept a property author or
reviewer reasons about. Making scope an explicit, reported tag turns an
implicit mechanical guard into a visible modeling decision, and makes a
real gap like "every property in this set is Global-scoped; nothing was
specifically checked immediately after reset, or specifically during a
multi-cycle transaction" something this project can point at directly,
the same way `mutation_adequacy.py` points at an untested code path
instead of leaving "is my property set good enough" as a guess.
"""

from __future__ import annotations

from typing import Dict, List, Optional

PATTERNS: List[str] = [
    "Absence", "Existence", "Bounded Existence", "Universality",
    "Precedence", "Response", "Chain Precedence", "Chain Response",
]

PATTERN_DESCRIPTIONS: Dict[str, str] = {
    "Absence": "A given state/event never occurs within the scope.",
    "Existence": "A given state/event must occur at least once within the scope.",
    "Bounded Existence": "A given state/event occurs at most (or exactly) k times within the scope.",
    "Universality": "A given state/event holds throughout the scope.",
    "Precedence": "Event B may only occur if event A occurred first within the scope.",
    "Response": "If event A occurs within the scope, event B must eventually follow.",
    "Chain Precedence": "Precedence generalized to an ordered sequence of events, not just a single pair.",
    "Chain Response": "Response generalized to an ordered sequence of events, not just a single pair.",
}

SCOPES: List[str] = ["Global", "Before", "After", "Between", "After-Until"]

SCOPE_DESCRIPTIONS: Dict[str, str] = {
    "Global": "The entire execution -- the property must hold at every point, with no bounding event.",
    "Before": "From the start of execution up to (not including) a named bounding event.",
    "After": "From a named bounding event to the end of execution.",
    "Between": "From one named event to a second named event, inclusive of that window only.",
    "After-Until": "Like Between, but the window continues even if the second event never occurs.",
}

# Hardware-flavored example for each scope, used in prompts and docs so
# "After" doesn't stay an abstract software-verification term when the
# actual claims here are about clocked RTL.
SCOPE_HARDWARE_EXAMPLES: Dict[str, str] = {
    "Global": "\"grant is always one-hot or zero\" -- true at every cycle, no bounding event.",
    "Before": "\"fifo stays empty before the first write\" -- bounded by a first-write event.",
    "After": "\"the state machine reaches IDLE after reset deasserts\" -- bounded by reset release.",
    "Between": "\"busy stays asserted between a request being accepted and its response\" -- "
               "bounded by two named events on either side.",
    "After-Until": "\"once an error latches, it stays latched until a clear pulse (if any)\" -- "
                    "the window continues indefinitely if the clearing event never comes.",
}


def summarize_pattern_scope_coverage(properties: List[dict]) -> dict:
    """Count how many proposed/checked properties fall into each (pattern,
    scope) combination, and flag which PATTERNS and SCOPES have zero
    representation at all -- a real, visible gap in what kinds of claims
    were ever considered, the same way an untested branch is a real gap
    in a property set's mutation-adequacy score (see mutation_adequacy.py),
    just from the specification-taxonomy angle instead of the RTL-coverage
    angle. Missing a whole CATEGORY entirely (e.g. zero Precedence-pattern
    properties) is worth surfacing even though it's not a solver-verified
    result -- it's a structural completeness signal, not a proof.

    Properties without a recognized `pattern`/`scope` field (e.g.
    hand-written properties never routed through the LLM suggester) are
    counted under "Unclassified" rather than silently dropped or
    guessed at -- this module does not attempt to infer a pattern/scope
    from an arbitrary expression string, since a wrong guess would be
    worse than an honest "unclassified."
    """
    pattern_counts: Dict[str, int] = {p: 0 for p in PATTERNS}
    scope_counts: Dict[str, int] = {s: 0 for s in SCOPES}
    unclassified = 0

    for prop in properties:
        pattern = prop.get("pattern")
        scope = prop.get("scope")
        classified = False
        if pattern in pattern_counts:
            pattern_counts[pattern] += 1
            classified = True
        if scope in scope_counts:
            scope_counts[scope] += 1
            classified = True
        if not classified:
            unclassified += 1

    missing_patterns = [p for p, n in pattern_counts.items() if n == 0]
    missing_scopes = [s for s, n in scope_counts.items() if n == 0]

    notes: List[str] = []
    if missing_patterns:
        notes.append(
            f"No properties tagged with pattern(s) {missing_patterns} -- this doesn't mean the "
            "design has a bug, only that this KIND of claim was never considered. Worth a "
            "deliberate check: does this design have any real ordering constraint "
            "(Precedence/Chain Precedence), any claim about something eventually happening "
            "(Response/Chain Response), or a bound on how many times something occurs "
            "(Bounded Existence) that hasn't been captured as a property yet?"
        )
    if missing_scopes and missing_scopes != ["After-Until"]:
        # After-Until is genuinely rare in practice even for a thorough
        # property set (it's the least commonly needed scope even in
        # Dwyer's own original survey of real specifications) -- flagging
        # its absence alone would be noise, not a real signal.
        notes.append(
            f"No properties scoped as {missing_scopes} -- every property so far is either "
            "Global or otherwise doesn't isolate a specific window of execution (e.g. "
            "immediately after reset, or during a specific multi-cycle transaction). If this "
            "design has reset-recovery behavior or multi-cycle transactions with their own "
            "invariants, consider whether a Before/After/Between-scoped property is needed."
        )

    return {
        "pattern_counts": pattern_counts,
        "scope_counts": scope_counts,
        "unclassified": unclassified,
        "missing_patterns": missing_patterns,
        "missing_scopes": missing_scopes,
        "notes": notes,
    }
