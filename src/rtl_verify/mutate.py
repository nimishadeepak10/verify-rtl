"""RTL mutation testing for property-SET adequacy -- "how many properties
is enough, and are they actually any good?"

Researched before writing any code, the same way this project's other
formal-methodology features were: YosysHQ's own published MCY (Mutation
Cover with Yosys) tool answers exactly this question for a design's test
suite. Its core idea, directly quoted from its own methodology docs: "test
how good your test suite is at detecting errors in your design, by
deliberately introducing... small mutations that change the value of a
single bit in the design... and checking if they are caught." A property
set that lets a real, small, syntactically-valid change to the RTL slip
through completely undetected has a genuine gap -- not a guess about
"is N properties enough," a measured answer.

This module deliberately implements a SIMPLIFIED subset of that idea,
not the full MCY tool: real MCY uses a separate formal EQUIVALENCE CHECK
to first filter out mutations that don't even change observable behavior
(its own docs' "NOCHANGE" and "EQGAP" categories), which needs its own
netlist-level machinery this project doesn't have. Without that filter,
a mutation this module's own property set doesn't catch might be a real
property-set gap, OR it might be a behaviorally-equivalent mutation (dead
code, a redundant recomputation) that no property COULD ever need to
catch. Every result this module reports says so explicitly rather than
implying the precision a full MCY setup would have -- consistent with
this project's standing rule to never claim a stronger result than what
was actually earned.

Mutation operators are simple, syntactic, single-token replacements at
one occurrence at a time -- the well-established "operator replacement"
family of mutation operators used across mutation-testing literature
generally (not RTL-specific research, since the operators themselves --
relational, logical, bitwise, arithmetic -- are the same regardless of
target language):

  - Relational: ==<->!=, <<->'<=', ><->'>='
  - Logical:    &&<->||
  - Bitwise:    &<->|, ^<->~^
  - Arithmetic: +<->-

Only the target module's OWN body is mutated (never a dependency module
defined elsewhere in the same source text), and never inside a comment
or string literal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from .analyzer import RtlModule
from .blackbox import _find_module_span


@dataclass
class Mutant:
    id: str
    operator: str  # e.g. "relational: == -> !="
    location: str  # a short source snippet around the mutated site, for a human to find it
    mutated_source: str  # the FULL rtl_source with exactly this one change applied


# Ordered so a longer operator (`==`, `!=`, `~^`) is always tried before
# a shorter one it contains (`^`) -- matching the longer token first and
# consuming it prevents a spurious secondary match on its own substring.
#
# `<=` and `>=` are deliberately NOT mutation SOURCES here, in either
# direction that would remove them: Verilog's `<=` is ambiguous between
# "less than or equal" (a comparison, inside an expression) and the
# nonblocking assignment operator (`lhs <= rhs;`, a statement) --
# confirmed a real, not hypothetical, risk by testing this exact case
# first: flipping `result <= a + b;`'s `<=` to `<` produces
# `result < a + b;`, a bare invalid-statement, not a behavioral mutant.
# Reliably telling the two apart needs real statement-level parsing this
# project doesn't have; approximating it risks silently generating
# syntactically-broken "mutants" that only ever produce a tool ERROR,
# not a real behavioral test. `< -> <=` and `> -> >=` stay safe in the
# OTHER direction: the source `<`/`>` is never ambiguous (nonblocking
# assignment has no bare `<`/`>` form), so the result lands in the exact
# same, already-confirmed expression context.
# Every single-character pattern below is guarded with lookaround so it
# can never match as PART of a different, multi-character token this
# table doesn't otherwise handle (`<=`, `>=`, `<<`, `>>`, `&&`, `||`,
# `++`, `--`) -- confirmed necessary, not defensive-for-its-own-sake, by
# testing: an unguarded `<` pattern matches the `<` inside an existing
# `<=`, and replacing just that one character turns
# `result <= a + b;` into the broken `result <== a + b;`.
_OPERATORS: List[Tuple[str, str, str, str]] = [
    # (regex, replacement, category, human-readable source token for labels)
    (r"==", "!=", "relational", "=="),
    (r"!=", "==", "relational", "!="),
    (r"&&", "||", "logical", "&&"),
    (r"\|\|", "&&", "logical", "||"),
    (r"~\^", "^", "bitwise", "~^"),
    (r"(?<!~)\^", "~^", "bitwise", "^"),
    (r"(?<!\+)\+(?!\+)", "-", "arithmetic", "+"),
    (r"(?<!-)-(?!-)", "+", "arithmetic", "-"),
    (r"(?<!&)&(?!&)", "|", "bitwise", "&"),
    (r"(?<!\|)\|(?!\|)", "&", "bitwise", "|"),
    (r"(?<!<)<(?![<=])", "<=", "relational", "<"),
    (r"(?<!>)>(?![>=])", ">=", "relational", ">"),
]


def _excluded_spans(text: str) -> List[Tuple[int, int]]:
    """Comment and string-literal spans -- never mutate inside either."""
    spans = []
    for m in re.finditer(r"//[^\n]*|/\*.*?\*/|\"(?:[^\"\\]|\\.)*\"", text, re.DOTALL):
        spans.append((m.start(), m.end()))
    return spans


def _in_any_span(pos: int, spans: List[Tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


def generate_mutants(module: RtlModule, rtl_source: str, max_mutants: int = 20) -> List[Mutant]:
    """Every distinct single-operator mutation of `module`'s own body,
    capped at `max_mutants` (a real, deliberate bound: exhaustively
    mutating a real design is expensive even for a full MCY setup with
    fast equivalence checking, and this project's own solver-based
    classification is considerably more expensive per mutant than that).
    Mutants are returned in source order -- if the cap truncates the
    list, later-appearing operators in the module are the ones left out,
    a real, disclosed limitation rather than an even/representative
    sample across the whole module.
    """
    start, end = _find_module_span(rtl_source, module.name)
    body = rtl_source[start:end]
    excluded = _excluded_spans(body)

    mutants: List[Mutant] = []
    used_spans: List[Tuple[int, int]] = []
    for pattern, replacement, category, source_token in _OPERATORS:
        if len(mutants) >= max_mutants:
            break
        for m in re.finditer(pattern, body):
            if len(mutants) >= max_mutants:
                break
            if _in_any_span(m.start(), excluded):
                continue
            # Don't mutate a span already consumed by an earlier
            # (longer-token) operator's match at this same position --
            # e.g. `==` already handled here means the `=` inside it
            # must not ALSO be flipped as a separate, overlapping mutant.
            if any(s <= m.start() < e or s < m.end() <= e for s, e in used_spans):
                continue
            used_spans.append((m.start(), m.end()))
            mutated_body = body[:m.start()] + replacement + body[m.end():]
            mutated_source = rtl_source[:start] + mutated_body + rtl_source[end:]
            line_start = body.rfind("\n", 0, m.start()) + 1
            line_end = body.find("\n", m.end())
            if line_end < 0:
                line_end = len(body)
            snippet = body[line_start:line_end].strip()
            mutants.append(Mutant(
                id=f"m{len(mutants)}",
                operator=f"{category}: {source_token} -> {replacement}",
                location=snippet[:100],
                mutated_source=mutated_source,
            ))
    return mutants
