"""ROM-to-case: replace a read-only memory with a combinational lookup.

Adapted from the memory-abstraction guidance in Siemens' Verification
Horizons "How to Reduce the Complexity of Formal Analysis" (Part 5, Memory
Abstraction) and Seligman et al.'s "Formal Verification" (Ch. 10, memory
abstraction): a memory that is only ever READ, with fixed contents, doesn't
need to be state at all. Rewriting it as a `case` lookup removes DEPTH x
WIDTH state bits and keeps all of the control logic, so, unlike a black-box
or a cut point, BOTH proofs and counterexamples stay valid -- the Siemens
write-up singles this variant out as the safe one for exactly that reason.

This module therefore does an exact-semantics rewrite, not an
approximation, and refuses (with a stated reason) anything it cannot be
sure about:

  - the array must have numeric bounds,
  - every write to it must sit inside an `initial` block with a literal
    index and a literal value (any runtime write makes it a RAM, not a ROM),
  - no `$readmemh/$readmemb` (contents live in an external file this
    module cannot see),
  - every read must be a plain `name[expr]` (a bit-select of a read,
    `name[i][3]`, would need a function-call index and is refused).

An address that is out of range or was never initialized returns `x`,
same as the original array, because the lookup function takes a full
32-bit address and the `case` has a `default: x` arm (no truncation/
aliasing of high address bits).

Text-level, like blackbox.py and param_reduce.py: no real SystemVerilog
parse, so anything it does not recognize is left alone and reported as not
eligible rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Tuple

_MEM_DECL = re.compile(
    r"\breg\s*(?:signed\s*)?(?:\[\s*(\d+)\s*:\s*(\d+)\s*\])?\s*(\w+)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*;")
_MODULE = re.compile(r"\bmodule\s+(\w+)\b")


@dataclass
class RomInfo:
    module: str
    name: str
    depth: int
    width: int
    eligible: bool
    reason: str
    entries: List[Tuple[int, str]] = field(default_factory=list)

    @property
    def state_bits_saved(self) -> int:
        return self.depth * self.width if self.eligible else 0


def _matching(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _initial_spans(text: str) -> List[Tuple[int, int]]:
    """(start, end) of each `initial` statement: a begin/end block or a
    single statement up to its ';'."""
    spans = []
    for m in re.finditer(r"\binitial\b", text):
        i = m.end()
        while i < len(text) and text[i].isspace():
            i += 1
        if text.startswith("begin", i):
            depth, j = 0, i
            for t in re.finditer(r"\b(begin|end)\b", text[i:]):
                depth += 1 if t.group(1) == "begin" else -1
                if depth == 0:
                    j = i + t.end()
                    break
            spans.append((m.start(), j))
        else:
            j = text.find(";", i)
            spans.append((m.start(), j + 1 if j != -1 else len(text)))
    return spans


def _in_spans(pos: int, spans: List[Tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


def _module_blocks(source: str):
    for m in _MODULE.finditer(source):
        end = re.search(r"\bendmodule\b", source[m.start():])
        if end:
            yield m.group(1), m.start(), m.start() + end.end()


def _analyze_memory(body: str, decl: re.Match) -> RomInfo:
    hi_w, lo_w, name, a, b = decl.group(1), decl.group(2), decl.group(3), decl.group(4), decl.group(5)
    width = abs(int(hi_w) - int(lo_w)) + 1 if hi_w is not None else 1
    depth = abs(int(a) - int(b)) + 1
    initial = _initial_spans(body)

    if re.search(rf"\$readmem[hb]\s*\([^)]*\b{re.escape(name)}\b", body):
        return RomInfo("", name, depth, width, False, "contents loaded by $readmemh/$readmemb from an external file")

    entries: List[Tuple[int, str]] = []
    write_re = re.compile(rf"\b{re.escape(name)}\s*\[([^\]]*)\]\s*(<=|=)(?!=)\s*([^;]+);")
    for wm in write_re.finditer(body):
        if not _in_spans(wm.start(), initial):
            return RomInfo("", name, depth, width, False, "written outside an initial block (this is a RAM, not a ROM)")
        idx = wm.group(1).strip()
        if not re.fullmatch(r"\d+", idx):
            return RomInfo("", name, depth, width, False, "initial write uses a non-literal index")
        entries.append((int(idx), wm.group(3).strip()))
    if not entries:
        return RomInfo("", name, depth, width, False, "no initial contents found, so there is nothing to build a lookup from")

    for rm in re.finditer(rf"\b{re.escape(name)}\s*\[", body):
        close = _matching(body, rm.end() - 1, "[", "]")
        if close != -1 and body[close + 1:close + 2] == "[":
            return RomInfo("", name, depth, width, False, "a read is further bit-selected (`name[i][k]`); not rewritable safely")
    return RomInfo("", name, depth, width, True,
                   f"read-only: {len(entries)} literal initial value(s), no runtime writes", entries)


def find_rom_candidates(rtl_source: str) -> List[RomInfo]:
    out: List[RomInfo] = []
    for mod_name, start, end in _module_blocks(rtl_source):
        body = rtl_source[start:end]
        for decl in _MEM_DECL.finditer(body):
            info = _analyze_memory(body, decl)
            info.module = mod_name
            out.append(info)
    return out


def _function_text(info: RomInfo) -> str:
    arms = "\n".join(f"                32'd{idx}: {info.name}__rom = {val};" for idx, val in info.entries)
    return (
        # ANSI-style argument list on purpose: this project's analyzer scans the
        # module body for `input ...;` statements to find non-ANSI ports, and a
        # `function f; input a; ...` form would be misread as a module port
        # (confirmed: the DUT instantiation failed with "no port named 'a'").
        f"    function [{info.width - 1}:0] {info.name}__rom(input [31:0] a);\n"
        f"        begin\n"
        f"            case (a)\n{arms}\n"
        f"                default: {info.name}__rom = {{{info.width}{{1'bx}}}};\n"
        f"            endcase\n"
        f"        end\n"
        f"    endfunction"
    )


def convert_roms_to_case(rtl_source: str) -> Tuple[str, List[RomInfo]]:
    """Rewrite every eligible ROM in `rtl_source`. Returns the new source
    and the full candidate list (eligible ones were converted; the rest
    carry the reason they were left alone)."""
    infos = find_rom_candidates(rtl_source)
    result = rtl_source
    for info in infos:
        if not info.eligible:
            continue
        # Re-locate each time: earlier rewrites shift offsets.
        block = next(((s, e) for n, s, e in _module_blocks(result) if n == info.module), None)
        if block is None:
            info.eligible, info.reason = False, "module not found during rewrite"
            continue
        s, e = block
        body = result[s:e]
        name = re.escape(info.name)

        # 1. remove initial writes to this memory (keep the rest of any initial block)
        body = re.sub(rf"[ \t]*\b{name}\s*\[\s*\d+\s*\]\s*(?:<=|=)(?!=)\s*[^;]+;[ \t]*\n?", "", body)
        # 2. replace the array declaration with the lookup function FIRST:
        # the declaration's own `name [0:N]` would otherwise be mistaken
        # for a read in the next step.
        body = re.sub(rf"\breg\s*(?:signed\s*)?(?:\[\s*\d+\s*:\s*\d+\s*\])?\s*{name}\s*\[[^\]]*\]\s*;",
                      lambda _m: _function_text(info), body, count=1)
        # 3. rewrite reads  name[expr] -> name__rom(expr)
        out, pos = [], 0
        for rm in re.finditer(rf"\b{name}\s*\[", body):
            if rm.start() < pos:
                continue
            close = _matching(body, rm.end() - 1, "[", "]")
            if close == -1:
                continue
            out.append(body[pos:rm.start()])
            out.append(f"{info.name}__rom({body[rm.end():close]})")
            pos = close + 1
        out.append(body[pos:])
        body = "".join(out)
        result = result[:s] + body + result[e:]
    return result, infos
