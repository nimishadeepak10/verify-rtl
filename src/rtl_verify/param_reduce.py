"""Parameter / structural reduction: re-run a proof on a smaller
configuration of the same design (FIFO depth 1024 -> 4, a 600-stage
pipeline -> 4 stages).

Adapted from the structural-reduction and parameter-reduction techniques in
Seligman et al.'s "Formal Verification" (Ch. 6 "structural abstraction" and
Ch. 10 "parameters and size reduction"), the Siemens Verification Horizons
complexity series, and the FVM complexity guide. The book's point beyond
raw speed is worth keeping: shrinking a queue from 40 to 4 entries lets
the solver reach the interesting "queue full" corner at a much lower
bound, so a reduced configuration is often both cheaper AND shallower to
the behaviors that matter.

THIS IS NOT SOUND FOR THE FULL CONFIGURATION, unlike black-boxing or cut
points, and every result says so. It over-constrains by replacing the real
design with a smaller one: a PROVEN verdict holds for the reduced
configuration only (a bug that needs the real size, e.g. a pointer-wrap
bug at a specific depth, can be missed), and a FALSIFIED verdict usually
carries over but isn't guaranteed to. It is reported as a bounded-
configuration result, never as a full proof.

Because of that, the AUTO path only proposes parameters whose names say
they count structure (depth, stages, entries, ...), never data widths --
shrinking a data width is only sound when data never steers control (the
book's "data independence" condition), which this module does not check.
A width can still be reduced by naming it explicitly in `param_overrides`.

What is rewritten, exactly (text-level, like blackbox.py):
  - a literal default in the target module's own parameter declarations
    (`#(parameter DEPTH = 1024)` or a body `parameter DEPTH = 1024;`)
  - a literal value in an instantiation's parameter override list inside
    the target module (`foo #(.STAGES(600)) u_foo (...)`)
Expression-valued parameters and `localparam`s are never touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .analyzer import find_hash_paren_close
from .blackbox import _find_module_span

_STRUCTURE_NAME = re.compile(
    r"(depth|stages?|entries|size|len(gth)?|num|count|words|slots|ways|sets|lanes|"
    r"channels|threads|ports|masters|slaves|clients|requestors)", re.IGNORECASE)
_WIDTH_NAME = re.compile(r"(width|^w$|bits|^dw$|^aw$|data_w|addr_w)", re.IGNORECASE)
_INT_LITERAL = re.compile(r"^\s*(\d+)\s*$")


@dataclass
class ParamReduction:
    name: str
    original: int
    proposed: int
    kind: str  # "structure" | "width" | "other"
    where: str  # "module_default" | "instance_override"
    reason: str


def _kind_of(name: str) -> str:
    if _STRUCTURE_NAME.search(name):
        return "structure"
    if _WIDTH_NAME.search(name):
        return "width"
    return "other"


def _split_top_level_commas(text: str) -> List[Tuple[int, int]]:
    """(start, end) spans of comma-separated items at nesting depth 0."""
    spans, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            spans.append((start, i))
            start = i + 1
    spans.append((start, len(text)))
    return spans


def _declaration_regions(mod_text: str) -> List[Tuple[int, int]]:
    """Regions of `mod_text` holding module-level parameter declarations:
    the `#( ... )` header list and each body `parameter ... ;` statement.
    """
    regions: List[Tuple[int, int]] = []
    m = re.match(r"\s*module\s+\w+\s*", mod_text)
    header_end = 0
    if m:
        idx = m.end()
        if idx < len(mod_text) and mod_text[idx] == "#":
            close = find_hash_paren_close(mod_text, idx)
            open_paren = mod_text.index("(", idx)
            regions.append((open_paren + 1, close))
            header_end = close
    for pm in re.finditer(r"\bparameter\b", mod_text[header_end:]):
        start = header_end + pm.start()
        end = mod_text.find(";", start)
        if end != -1:
            regions.append((start, end))
    return regions


def _instance_override_regions(mod_text: str) -> List[Tuple[int, int]]:
    """Inner regions of every `#( ... )` after the module header (i.e.
    instantiation parameter override lists)."""
    regions: List[Tuple[int, int]] = []
    m = re.match(r"\s*module\s+\w+\s*", mod_text)
    pos = m.end() if m else 0
    if m and pos < len(mod_text) and mod_text[pos] == "#":
        pos = find_hash_paren_close(mod_text, pos) + 1
    for hm in re.finditer(r"#\s*\(", mod_text[pos:]):
        hash_idx = pos + hm.start()
        try:
            close = find_hash_paren_close(mod_text, hash_idx)
        except ValueError:
            continue
        regions.append((mod_text.index("(", hash_idx) + 1, close))
    return regions


_ITEM_NAME_VALUE = re.compile(r"(?:\bparameter\b[^=]*?)?\b([A-Za-z_]\w*)\s*=\s*([^,;]+)$", re.DOTALL)
_OVERRIDE = re.compile(r"\.\s*([A-Za-z_]\w*)\s*\(\s*(\d+)\s*\)")


def _scan(mod_text: str):
    """Yield (name, int_value, value_start, value_end, where) for every
    literal-valued parameter occurrence in `mod_text`.
    """
    for r_start, r_end in _declaration_regions(mod_text):
        region = mod_text[r_start:r_end]
        for s, e in _split_top_level_commas(region):
            item = region[s:e]
            im = _ITEM_NAME_VALUE.search(item.rstrip())
            if not im:
                continue
            vm = _INT_LITERAL.match(im.group(2))
            if not vm:
                continue
            v_start = r_start + s + im.start(2)
            v_end = r_start + s + im.end(2)
            yield im.group(1), int(vm.group(1)), v_start, v_end, "module_default"
    for r_start, r_end in _instance_override_regions(mod_text):
        for om in _OVERRIDE.finditer(mod_text[r_start:r_end]):
            yield (om.group(1), int(om.group(2)),
                   r_start + om.start(2), r_start + om.end(2), "instance_override")


def list_parameters(rtl_source: str, module_name: str) -> List[Tuple[str, int, str]]:
    start, end = _find_module_span(rtl_source, module_name)
    return [(n, v, w) for n, v, _s, _e, w in _scan(rtl_source[start:end])]


def recommend_parameter_reductions(
    rtl_source: str, module_name: str, min_value: int = 16, target: int = 4
) -> List[ParamReduction]:
    """Structure-named literal parameters >= `min_value`, largest first.
    Widths and unclassified names are never proposed automatically.
    """
    out: List[ParamReduction] = []
    seen: set = set()
    for name, value, where in list_parameters(rtl_source, module_name):
        if (name, where) in seen or value < min_value:
            continue
        kind = _kind_of(name)
        if kind != "structure":
            continue
        seen.add((name, where))
        out.append(ParamReduction(
            name=name, original=value, proposed=min(target, value), kind=kind, where=where,
            reason=(f"'{name}' = {value} counts structure (depth/stages/entries-like); a "
                    f"{min(target, value)}-element version reaches full/empty/wrap corners at a "
                    "much lower proof bound."),
        ))
    out.sort(key=lambda r: r.original, reverse=True)
    return out


def apply_parameter_overrides(
    rtl_source: str, module_name: str, overrides: Dict[str, int]
) -> Tuple[str, List[dict], List[str]]:
    """Rewrite literal parameter values named in `overrides` inside the
    target module only. Returns (new_source, applied, missing_names): a
    name that matched nothing is reported, never silently ignored.
    """
    start, end = _find_module_span(rtl_source, module_name)
    mod_text = rtl_source[start:end]
    edits = []
    applied: List[dict] = []
    matched: set = set()
    for name, value, v_start, v_end, where in _scan(mod_text):
        if name in overrides:
            edits.append((v_start, v_end, str(overrides[name])))
            applied.append({"name": name, "from": value, "to": overrides[name], "where": where})
            matched.add(name)
    for v_start, v_end, text in sorted(edits, reverse=True):
        mod_text = mod_text[:v_start] + text + mod_text[v_end:]
    missing = [n for n in overrides if n not in matched]
    return rtl_source[:start] + mod_text + rtl_source[end:], applied, missing


def parse_override_spec(spec: str) -> Tuple[Dict[str, int], List[str]]:
    """'DEPTH=4, STAGES=8' -> ({'DEPTH': 4, 'STAGES': 8}, errors)."""
    overrides: Dict[str, int] = {}
    errors: List[str] = []
    for item in [s.strip() for s in spec.split(",") if s.strip()]:
        m = re.fullmatch(r"([A-Za-z_]\w*)\s*=\s*(\d+)", item)
        if not m:
            errors.append(f"'{item}' is not NAME=<non-negative integer>")
        else:
            overrides[m.group(1)] = int(m.group(2))
    return overrides, errors
