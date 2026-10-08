"""Static lint rules for assert/assume/cover statements, researched from
real false-positive post-mortems in Seligman, Schubert & Achutha Kiran
Kumar's "Formal Verification" (Chapter 9, "Formal Verification's Greatest
Bloopers"). Every rule below targets a specific, named failure mode that
book documents as having actually reached tapeout at Intel because the
assertion silently checked something other than what its author intended
while still LOOKING like it was working (still catching some real bugs,
still showing PASS/FAIL activity) -- the exact "silent partial coverage
loss" pattern this project's other checks (vacuity.py, assumption_
check.py) also exist to catch, but from the RTL text itself rather than
from a solver run. Because these are purely textual/structural checks,
they run instantly with no formal engine involved, and catch a
complementary class of bug: a property that COMPILES and PROVES, but
doesn't mean what its author thinks it means.

Rules implemented, each mapped to the book's real-case section:

  - missing_semicolon: an assert/assume/cover whose condition isn't
    followed by `;` or `else` -- in SVA, the next statement silently
    becomes this assertion's PASS-action clause instead of running on its
    own every cycle (the book's "Missing Semicolon" case, a real Intel
    bug escape found by a validation engineer at a late project stage).

  - short_circuit_assertion_function: a function containing an assert/
    assume/cover, called from the non-first operand of `||` or `&&`
    elsewhere -- SystemVerilog's short-circuit evaluation rules mean the
    call (and its assertion) may never execute (the book's "Short-
    Circuited Function with Assertion" case).

  - moving_sampled_index: a $past/$stable/$rose/$fell/$changed call whose
    argument indexes an array with a non-constant, non-genvar signal --
    the sampled-value function uses last cycle's value of BOTH the base
    signal and the index together, so a moving index compares data from
    two different array positions across two different cycles (the
    book's "Subtle Effects of Signal Sampling" case, generalized from
    concurrent-assertion syntax to this project's $past()-based immediate-
    assertion style, see docs/formal_property_reference.md).

  - generate_label_collision: a statically labeled assert/assume/cover
    inside a `generate for` loop driven by a genvar -- a CONFIRMED
    limitation of this project's own yosys build (see the MESI repo's
    "Notable findings" section), not a hypothetical from the book: such a
    label is treated as a flat cell name rather than scoped per generate
    instance, so every iteration after the first fails to elaborate.

Two rules from the book's chapter were deliberately NOT implemented
because they target concurrent SVA syntax (`assert property (@(...) ...)`)
that this project's own yosys build rejects outright (confirmed, see the
MESI repo's "Notable findings" section and docs/formal_property_
reference.md): "Assertion at Both Clock Edges" and "Liveness Properties
that are Not Alive" (weak vs. strong `s_eventually`). Neither construct
can appear in any RTL this project's tools will even compile, so a lint
rule for them would never fire and would only give false confidence that
this class of bug had been checked for.

This is a static, textual scan -- not a real SystemVerilog parse. Like
this project's other static checks (signal_coverage.py, mutate.py), it
can miss cases hidden behind unusual formatting and, in rare cases, flag
something that isn't really a bug; every finding names the exact line and
snippet so a human can confirm it quickly rather than trusting the label
alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Set

_ASSERTION_KEYWORDS = ("assert", "assume", "cover")
_SAMPLED_VALUE_FUNCTIONS = ("$past", "$stable", "$rose", "$fell", "$changed")


@dataclass
class LintFinding:
    rule: str
    severity: str  # "error" | "warning"
    line: int
    snippet: str
    message: str


@dataclass
class LintReport:
    findings: List[LintFinding] = field(default_factory=list)
    note: str = ""


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _snippet_of(text: str, start: int, end: int, max_len: int = 90) -> str:
    s = text[start:end].strip()
    s = re.sub(r"\s+", " ", s)
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def _find_matching_paren(text: str, open_idx: int) -> int:
    """`text[open_idx]` must be '('. Returns the index of its matching ')',
    or -1 if unbalanced.
    """
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _strip_comments(source: str) -> str:
    """Blank out (same length, so offsets/lines stay valid) //... and
    /*...*/ comments so they can't trigger false matches, without
    disturbing any character positions used for line-number reporting.
    """
    out = list(source)
    i = 0
    n = len(source)
    while i < n:
        if source[i : i + 2] == "//":
            j = source.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif source[i : i + 2] == "/*":
            j = source.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _find_assertion_statements(clean: str):
    """Yield (keyword, kw_start, cond_open, cond_close) for every top-level
    assert/assume/cover statement -- skips `assert property (`'s own
    `property` token and an optional `final`/`#0` deferred-assertion
    modifier to land on the real condition's parens.
    """
    for m in re.finditer(r"\b(assert|assume|cover)\b", clean):
        kw_start = m.start()
        pos = m.end()
        # optional modifiers: `final`, `#0`, `property`
        mod_match = re.match(r"\s*(final\s+|#0\s*|property\s*)*", clean[pos:])
        pos += mod_match.end() if mod_match else 0
        paren = clean.find("(", pos)
        if paren == -1 or paren - pos > 3:
            # not immediately an assertion condition (e.g. a plain
            # identifier named "cover" used elsewhere, or a declaration)
            continue
        close = _find_matching_paren(clean, paren)
        if close == -1:
            continue
        yield m.group(1), kw_start, paren, close


def _check_missing_semicolon(source: str, clean: str) -> List[LintFinding]:
    findings: List[LintFinding] = []
    for keyword, kw_start, _open, close in _find_assertion_statements(clean):
        rest = clean[close + 1 :]
        stripped = rest.lstrip()
        skipped = len(rest) - len(stripped)
        if stripped.startswith(";"):
            continue
        if re.match(r"else\b", stripped):
            # has a fail-action clause; that clause itself must still end
            # in ';' before the next assertion-like keyword, so keep
            # scanning past 'else' for the actual terminator.
            after_else = stripped[len("else") :]
            # Fail action is typically a single statement (often
            # $error(...)) ending in ';'; find the first top-level ';'
            # scanning past any balanced parens it may contain.
            depth = 0
            terminated = False
            for i, ch in enumerate(after_else):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == ";" and depth <= 0:
                    terminated = True
                    break
                elif re.match(r"\b(assert|assume|cover)\b", after_else[i:]) and depth <= 0:
                    break
            if terminated:
                continue
        line = _line_of(source, kw_start)
        snippet = _snippet_of(source, kw_start, min(close + 40, len(source)))
        findings.append(
            LintFinding(
                rule="missing_semicolon",
                severity="error",
                line=line,
                snippet=snippet,
                message=(
                    f"This '{keyword}' statement is not terminated by ';' (and has no "
                    "'else' clause) before the next code. In SVA, whatever statement "
                    "comes next -- even another assert/assume/cover -- is silently "
                    "parsed as THIS assertion's pass-action, and will only run when "
                    "this assertion itself passes, not on its own every cycle. Add the "
                    "missing semicolon."
                ),
            )
        )
    return findings


def _collect_functions_with_assertions(clean: str) -> Set[str]:
    names: Set[str] = set()
    for m in re.finditer(r"\bfunction\b[^;]*?\b(\w+)\s*\(", clean):
        fname = m.group(1)
        end = clean.find("endfunction", m.end())
        if end == -1:
            continue
        body = clean[m.end() : end]
        if re.search(r"\b(assert|assume|cover)\b\s*(final\s+|#0\s*|property\s*)*\(", body):
            names.add(fname)
    return names


def _check_short_circuit_functions(source: str, clean: str) -> List[LintFinding]:
    findings: List[LintFinding] = []
    risky_functions = _collect_functions_with_assertions(clean)
    if not risky_functions:
        return findings
    for fname in sorted(risky_functions):
        # A call to fname(...) immediately preceded by '||' or '&&' (i.e.
        # not the left-hand/first term of the short-circuiting operator)
        # may never execute if the earlier operand already decides the
        # expression's result.
        pattern = re.compile(r"(\|\||&&)\s*!?\s*\b" + re.escape(fname) + r"\s*\(")
        for m in pattern.finditer(clean):
            line = _line_of(source, m.start())
            snippet = _snippet_of(source, m.start(), min(m.end() + 40, len(source)))
            findings.append(
                LintFinding(
                    rule="short_circuit_assertion_function",
                    severity="warning",
                    line=line,
                    snippet=snippet,
                    message=(
                        f"Function '{fname}' contains an assert/assume/cover, and is "
                        "called here as a non-first operand of '||' or '&&'. "
                        "SystemVerilog short-circuit evaluation means this call, and "
                        "the assertion inside it, may be silently skipped whenever the "
                        "earlier operand already determines the expression's result. "
                        "Move this call to be evaluated unconditionally (e.g. assign "
                        "its result to an intermediate signal first) if the assertion "
                        "must always be checked."
                    ),
                )
            )
    return findings


def _collect_safe_index_names(clean: str) -> Set[str]:
    """genvar names -- a compile-time-constant loop index is NOT the
    moving-index hazard this rule targets, since each generate instance
    gets its own fixed index, not a runtime-varying one.
    """
    names: Set[str] = set()
    for m in re.finditer(r"\bgenvar\s+(\w+)", clean):
        names.add(m.group(1))
    for m in re.finditer(r"\bfor\s*\(\s*genvar\s+(\w+)", clean):
        names.add(m.group(1))
    return names


def _check_moving_sampled_index(source: str, clean: str) -> List[LintFinding]:
    findings: List[LintFinding] = []
    safe_indices = _collect_safe_index_names(clean)
    for fn in _SAMPLED_VALUE_FUNCTIONS:
        for m in re.finditer(re.escape(fn) + r"\s*\(", clean):
            open_idx = m.end() - 1
            close_idx = _find_matching_paren(clean, open_idx)
            if close_idx == -1:
                continue
            arg = clean[open_idx + 1 : close_idx]
            for idx_m in re.finditer(r"\[\s*([^\[\]]+?)\s*\]", arg):
                index_expr = idx_m.group(1).strip()
                if re.fullmatch(r"[0-9]+|[0-9]+'[bhdo][0-9a-fA-F_]+", index_expr):
                    continue  # constant literal index, not a hazard
                idents = re.findall(r"\b[A-Za-z_]\w*\b", index_expr)
                if idents and all(ident in safe_indices for ident in idents):
                    continue  # genvar-only index, safe per generate instance
                line = _line_of(source, m.start())
                snippet = _snippet_of(source, m.start(), close_idx + 1)
                findings.append(
                    LintFinding(
                        rule="moving_sampled_index",
                        severity="warning",
                        line=line,
                        snippet=snippet,
                        message=(
                            f"{fn}(...) samples its whole argument as of last cycle, "
                            f"including the array index '{index_expr}'. If '{index_expr}' "
                            "is a runtime signal that can itself change value, this "
                            "compares last cycle's index against this cycle's (or vice "
                            "versa), not the same array element across both cycles -- a "
                            "real false-negative/false-positive hazard, not a style issue. "
                            "If the index truly never changes at runtime (e.g. it is a "
                            "genvar), this is a false positive and can be ignored."
                        ),
                    )
                )
    return findings


def _check_generate_label_collision(source: str, clean: str) -> List[LintFinding]:
    findings: List[LintFinding] = []
    genvar_names = _collect_safe_index_names(clean)
    if not genvar_names:
        return findings

    # Find each `generate ... endgenerate` span (non-nested match; this
    # project's own RTL never nests one generate block inside another).
    spans = []
    starts = [m.start() for m in re.finditer(r"\bgenerate\b", clean)]
    for start in starts:
        end = clean.find("endgenerate", start)
        if end != -1:
            spans.append((start, end))

    for span_start, span_end in spans:
        block = clean[span_start:span_end]
        for_match = re.search(r"\bfor\s*\(\s*(?:genvar\s+)?(\w+)\s*=", block)
        if not for_match or for_match.group(1) not in genvar_names:
            continue
        for label_m in re.finditer(r"\b(\w+)\s*:\s*(assert|assume|cover)\b", block):
            abs_pos = span_start + label_m.start()
            line = _line_of(source, abs_pos)
            snippet = _snippet_of(source, abs_pos, min(abs_pos + 60, len(source)))
            findings.append(
                LintFinding(
                    rule="generate_label_collision",
                    severity="error",
                    line=line,
                    snippet=snippet,
                    message=(
                        f"Static label '{label_m.group(1)}' on this {label_m.group(2)} "
                        "is inside a genvar-driven generate-for loop. This project's own "
                        "yosys build has a confirmed limitation where such a label is "
                        "treated as a flat cell name rather than scoped per generate "
                        "instance, so every iteration after the first fails to "
                        "elaborate (see the MESI project's 'Notable findings' section). "
                        "Remove the label -- SymbiYosys still reports the failing "
                        "instance via its hierarchical path in any counterexample."
                    ),
                )
            )
    return findings


def lint_sva(rtl_source: str) -> LintReport:
    """Run every static SVA lint rule against `rtl_source` and return a
    single combined report.
    """
    clean = _strip_comments(rtl_source)
    findings: List[LintFinding] = []
    findings += _check_missing_semicolon(rtl_source, clean)
    findings += _check_short_circuit_functions(rtl_source, clean)
    findings += _check_moving_sampled_index(rtl_source, clean)
    findings += _check_generate_label_collision(rtl_source, clean)
    findings.sort(key=lambda f: f.line)

    if not findings:
        note = (
            "No SVA lint issues found by these 4 static rules. This is a textual "
            "scan, not a full SystemVerilog parse -- it complements, and does not "
            "replace, a real proof run or a manual assertion review."
        )
    else:
        errors = sum(1 for f in findings if f.severity == "error")
        warnings = sum(1 for f in findings if f.severity == "warning")
        note = (
            f"{len(findings)} finding(s): {errors} error(s), {warnings} warning(s). "
            "Each is a documented real false-positive/false-negative pattern, not a "
            "style preference -- see each finding's message for why it matters."
        )
    return LintReport(findings=findings, note=note)
