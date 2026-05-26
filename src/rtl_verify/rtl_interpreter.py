"""
RTL interpreter (coverage-only).

This module **does not simulate** RTL to produce outputs. Instead, it:
- parses a small, common subset of Verilog into a lightweight AST with source line numbers
- "executes" that AST across a VCD-derived timeline by evaluating *conditions* (if/case)
  from signal values in the VCD

This provides statement + branch coverage without modifying RTL and without requiring
external tools/instrumentation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class StatementRef:
    line_no: int
    text: str = ""


@dataclass
class BranchArm:
    label: str
    line_no: int
    hit_count: int = 0


@dataclass
class BranchSite:
    site_id: str
    kind: str  # "if" | "case"
    line_no: int
    expr: str
    location: str
    arms: List[BranchArm] = field(default_factory=list)


@dataclass
class ParseResult:
    statements: List[StatementRef]
    branch_sites: List[BranchSite]
    always_blocks: List[dict]


_RE_ASSIGN = re.compile(r"^\s*assign\s+(\w+)\s*=\s*([^;]+);\s*$", re.IGNORECASE)
_RE_ASSIGN_START = re.compile(r"^\s*assign\b", re.IGNORECASE)
_RE_ALWAYS = re.compile(r"^\s*always\s*@\s*\(\s*\*\s*\)\s*begin\s*$", re.IGNORECASE)
_RE_END = re.compile(r"^\s*end\s*$", re.IGNORECASE)
_RE_CASE = re.compile(r"^\s*case\s*\(\s*([^)]+)\s*\)\s*$", re.IGNORECASE)
_RE_ENDCASE = re.compile(r"^\s*endcase\s*$", re.IGNORECASE)
_RE_IF = re.compile(r"^\s*if\s*\(\s*(.+?)\s*\)\s*begin\s*$", re.IGNORECASE)
_RE_ELSE_BEGIN = re.compile(r"^\s*else\s*begin\s*$", re.IGNORECASE)
_RE_LABEL = re.compile(r"^\s*([^:]+)\s*:\s*begin\s*$", re.IGNORECASE)
_RE_DEFAULT = re.compile(r"^\s*default\s*:\s*begin\s*$", re.IGNORECASE)
_RE_STMT_ASSIGN = re.compile(r"^\s*\w+\s*(?:<=|=)\s*[^;]+;\s*$")


def _strip_comments_keep_lines(rtl: str) -> List[str]:
    # remove /* */ blocks but preserve line count
    def repl_block(m: re.Match[str]) -> str:
        s = m.group(0)
        return "\n" * s.count("\n")

    rtl2 = re.sub(r"/\*.*?\*/", repl_block, rtl, flags=re.DOTALL)
    lines: List[str] = []
    for ln in rtl2.splitlines():
        lines.append(re.sub(r"//.*$", "", ln))
    return lines


def parse_rtl_for_coverage(rtl: str, filename: str = "dut.v") -> ParseResult:
    """
    Parse subset of Verilog for coverage.

    Statement counting:
    - each `assign ...;` line is a statement
    - inside `always @(*) begin ... end`, each assignment statement line is a statement
      (case/if headers are NOT statements)
    """
    lines = _strip_comments_keep_lines(rtl)
    statements: List[StatementRef] = []
    branch_sites: List[BranchSite] = []
    always_blocks: List[dict] = []

    # Always blocks are parsed shallowly with a stack.
    i = 0
    site_seq = 0

    def new_site_id(kind: str) -> str:
        nonlocal site_seq
        site_seq += 1
        return f"{kind}_{site_seq:04d}"

    while i < len(lines):
        raw = lines[i]
        line_no = i + 1
        s = raw.strip()

        m = _RE_ASSIGN.match(raw)
        if m:
            statements.append(StatementRef(line_no=line_no, text=s))
            i += 1
            continue

        # Multi-line assign: "assign x = ... \n ... ;"
        if _RE_ASSIGN_START.match(raw) and ";" not in raw:
            start_ln = line_no
            buf = [s]
            j = i + 1
            while j < len(lines):
                buf.append(lines[j].strip())
                if ";" in lines[j]:
                    break
                j += 1
            statements.append(StatementRef(line_no=start_ln, text=" ".join([b for b in buf if b])))
            i = j + 1
            continue

        if _RE_ALWAYS.match(raw):
            i += 1
            # parse until matching `end`
            block, i = _parse_always_block(
                lines, i, filename, new_site_id, branch_sites, statements
            )
            always_blocks.append(block)
            continue

        i += 1

    return ParseResult(
        statements=statements, branch_sites=branch_sites, always_blocks=always_blocks
    )


def _parse_always_block(
    lines: Sequence[str],
    start_idx: int,
    filename: str,
    new_site_id,
    branch_sites: List[BranchSite],
    statements: List[StatementRef],
) -> Tuple[dict, int]:
    """
    Returns (block_ast, next_idx_after_end)
    """
    i = start_idx
    items: List[dict] = []
    while i < len(lines):
        raw = lines[i]
        line_no = i + 1
        s = raw.strip()

        if _RE_END.match(raw):
            return {"kind": "always", "items": items, "line_no": line_no}, i + 1

        m = _RE_IF.match(raw)
        if m:
            expr = m.group(1).strip()
            site = BranchSite(
                site_id=new_site_id("if"),
                kind="if",
                line_no=line_no,
                expr=expr,
                location=f"{filename}:{line_no} if",
                arms=[
                    BranchArm(label="then", line_no=line_no),
                    BranchArm(label="else", line_no=line_no),
                ],
            )
            branch_sites.append(site)
            then_items, i = _parse_begin_end(lines, i + 1, filename, new_site_id, branch_sites, statements)
            else_items: List[dict] = []
            # optional else begin
            if i < len(lines) and _RE_ELSE_BEGIN.match(lines[i]):
                else_items, i = _parse_begin_end(lines, i + 1, filename, new_site_id, branch_sites, statements)
            items.append({"kind": "if", "site_id": site.site_id, "expr": expr, "then": then_items, "else": else_items, "line_no": line_no})
            continue

        m = _RE_CASE.match(raw)
        if m:
            expr = m.group(1).strip()
            site = BranchSite(
                site_id=new_site_id("case"),
                kind="case",
                line_no=line_no,
                expr=expr,
                location=f"{filename}:{line_no} case {expr}",
                arms=[],
            )
            branch_sites.append(site)
            case_items, i = _parse_case(lines, i + 1, filename, new_site_id, branch_sites, statements, site)
            items.append({"kind": "case", "site_id": site.site_id, "expr": expr, "arms": case_items, "line_no": line_no})
            continue

        if _RE_STMT_ASSIGN.match(raw):
            statements.append(StatementRef(line_no=line_no, text=s))
            items.append({"kind": "stmt", "line_no": line_no, "text": s})
            i += 1
            continue

        i += 1

    return {"kind": "always", "items": items, "line_no": start_idx + 1}, i


def _parse_begin_end(
    lines: Sequence[str],
    start_idx: int,
    filename: str,
    new_site_id,
    branch_sites: List[BranchSite],
    statements: List[StatementRef],
) -> Tuple[List[dict], int]:
    i = start_idx
    items: List[dict] = []
    while i < len(lines):
        raw = lines[i]
        line_no = i + 1
        s = raw.strip()
        if _RE_END.match(raw):
            return items, i + 1

        m = _RE_IF.match(raw)
        if m:
            expr = m.group(1).strip()
            site = BranchSite(
                site_id=new_site_id("if"),
                kind="if",
                line_no=line_no,
                expr=expr,
                location=f"{filename}:{line_no} if",
                arms=[BranchArm(label="then", line_no=line_no), BranchArm(label="else", line_no=line_no)],
            )
            branch_sites.append(site)
            then_items, i = _parse_begin_end(lines, i + 1, filename, new_site_id, branch_sites, statements)
            else_items: List[dict] = []
            if i < len(lines) and _RE_ELSE_BEGIN.match(lines[i]):
                else_items, i = _parse_begin_end(lines, i + 1, filename, new_site_id, branch_sites, statements)
            items.append({"kind": "if", "site_id": site.site_id, "expr": expr, "then": then_items, "else": else_items, "line_no": line_no})
            continue

        m = _RE_CASE.match(raw)
        if m:
            expr = m.group(1).strip()
            site = BranchSite(
                site_id=new_site_id("case"),
                kind="case",
                line_no=line_no,
                expr=expr,
                location=f"{filename}:{line_no} case {expr}",
                arms=[],
            )
            branch_sites.append(site)
            case_items, i = _parse_case(lines, i + 1, filename, new_site_id, branch_sites, statements, site)
            items.append({"kind": "case", "site_id": site.site_id, "expr": expr, "arms": case_items, "line_no": line_no})
            continue

        if _RE_STMT_ASSIGN.match(raw):
            statements.append(StatementRef(line_no=line_no, text=s))
            items.append({"kind": "stmt", "line_no": line_no, "text": s})
            i += 1
            continue

        i += 1
    return items, i


def _parse_case(
    lines: Sequence[str],
    start_idx: int,
    filename: str,
    new_site_id,
    branch_sites: List[BranchSite],
    statements: List[StatementRef],
    site: BranchSite,
) -> Tuple[List[dict], int]:
    i = start_idx
    arms: List[dict] = []
    while i < len(lines):
        raw = lines[i]
        line_no = i + 1
        if _RE_ENDCASE.match(raw):
            # implicit default arm if missing
            if not any(a.get("label") == "default" for a in arms):
                site.arms.append(BranchArm(label="default", line_no=line_no))
                arms.append({"label": "default", "items": [], "line_no": line_no})
            return arms, i + 1

        m = _RE_DEFAULT.match(raw)
        if m:
            site.arms.append(BranchArm(label="default", line_no=line_no))
            items, i = _parse_begin_end(lines, i + 1, filename, new_site_id, branch_sites, statements)
            arms.append({"label": "default", "items": items, "line_no": line_no})
            continue

        m = _RE_LABEL.match(raw)
        if m:
            label = m.group(1).strip()
            site.arms.append(BranchArm(label=label, line_no=line_no))
            items, i = _parse_begin_end(lines, i + 1, filename, new_site_id, branch_sites, statements)
            arms.append({"label": label, "items": items, "line_no": line_no})
            continue

        i += 1
    return arms, i


def _parse_literal(expr: str) -> Optional[int]:
    e = expr.strip()
    if re.fullmatch(r"\d+", e):
        return int(e, 10)
    m = re.fullmatch(r"(\d+)?'([bhdBHD])\s*([0-9a-fA-F_]+)", e)
    if m:
        width = int(m.group(1) or "32")
        base = m.group(2).lower()
        digits = m.group(3).replace("_", "")
        if base == "b":
            v = int(digits, 2)
        elif base == "h":
            v = int(digits, 16)
        else:
            v = int(digits, 10)
        return v & ((1 << width) - 1)
    return None


def _eval_bool(expr: str, env: Dict[str, Optional[int]]) -> Optional[bool]:
    """
    Evaluate a tiny subset of if conditions.
    Returns None if unknown (X/Z or unsupported).
    Supported:
    - signal
    - !signal
    - signal == literal / signal != literal
    """
    e = expr.strip()
    m = re.fullmatch(r"!\s*(\w+)", e)
    if m:
        v = env.get(m.group(1))
        return None if v is None else (v == 0)
    m = re.fullmatch(r"(\w+)\s*([!=]=)\s*(.+)", e)
    if m:
        sig = m.group(1)
        op = m.group(2)
        lit = _parse_literal(m.group(3))
        v = env.get(sig)
        if v is None or lit is None:
            return None
        return (v == lit) if op == "==" else (v != lit)
    m = re.fullmatch(r"(\w+)", e)
    if m:
        v = env.get(m.group(1))
        return None if v is None else (v != 0)
    return None


def _eval_int(expr: str, env: Dict[str, Optional[int]]) -> Optional[int]:
    e = expr.strip()
    lit = _parse_literal(e)
    if lit is not None:
        return lit
    m = re.fullmatch(r"(\w+)", e)
    if m:
        return env.get(m.group(1))
    return None


class CoverageInterpreter:
    def __init__(self, rtl_source: str, filename: str = "dut.v") -> None:
        self.rtl_source = rtl_source
        self.filename = filename
        pr = parse_rtl_for_coverage(rtl_source, filename=filename)
        self._statements = pr.statements
        self._branch_sites = pr.branch_sites
        self._always_blocks = pr.always_blocks
        self.hit_counter: Dict[int, int] = {}
        self._branch_by_id: Dict[str, BranchSite] = {s.site_id: s for s in self._branch_sites}

    @property
    def statements(self) -> List[StatementRef]:
        return list(self._statements)

    @property
    def branch_sites(self) -> List[BranchSite]:
        return list(self._branch_sites)

    def execute_over_timeline(self, env_by_time: Dict[int, Dict[str, Optional[int]]]) -> None:
        """
        env_by_time: mapping time -> {signal_name: int or None}
        """
        # `assign` statements are continuously active: count them once.
        for st in self._statements:
            # we'll increment as we encounter statements in always blocks;
            # but for top-level assign we want at least one hit.
            # heuristic: if statement text starts with assign, count it once.
            if st.text.lower().startswith("assign"):
                self.hit_counter[st.line_no] = self.hit_counter.get(st.line_no, 0) + 1

        times = sorted(env_by_time.keys())
        for t in times:
            env = env_by_time[t]
            for b in self._always_blocks:
                self._exec_items(b.get("items") or [], env)

    def _exec_items(self, items: List[dict], env: Dict[str, Optional[int]]) -> None:
        for it in items:
            kind = it.get("kind")
            if kind == "stmt":
                ln = int(it.get("line_no"))
                self.hit_counter[ln] = self.hit_counter.get(ln, 0) + 1
                continue
            if kind == "if":
                site = self._branch_by_id.get(it.get("site_id"))
                cond = _eval_bool(str(it.get("expr") or ""), env)
                taken_then = bool(cond) if cond is not None else False
                if site:
                    arm = site.arms[0] if taken_then else site.arms[1]
                    arm.hit_count += 1
                branch_items = it.get("then") if taken_then else it.get("else")
                self._exec_items(branch_items or [], env)
                continue
            if kind == "case":
                site = self._branch_by_id.get(it.get("site_id"))
                expr_val = _eval_int(str(it.get("expr") or ""), env)
                chosen = None
                for arm in it.get("arms") or []:
                    lab = str(arm.get("label"))
                    if lab == "default":
                        continue
                    lit = _parse_literal(lab)
                    if expr_val is not None and lit is not None and expr_val == lit:
                        chosen = arm
                        break
                if chosen is None:
                    chosen = next((a for a in (it.get("arms") or []) if a.get("label") == "default"), None)
                if site and chosen:
                    label = str(chosen.get("label"))
                    for a in site.arms:
                        if a.label == label:
                            a.hit_count += 1
                            break
                self._exec_items((chosen or {}).get("items") or [], env)
                continue

    def get_statement_coverage(self):
        from .coverage import StatementCoverage

        all_lines = sorted({s.line_no for s in self._statements})
        hit_counts = {ln: int(self.hit_counter.get(ln, 0)) for ln in all_lines}
        hit = sum(1 for ln in all_lines if hit_counts.get(ln, 0) > 0)
        uncovered = [ln for ln in all_lines if hit_counts.get(ln, 0) == 0]
        return StatementCoverage(
            total=len(all_lines),
            hit=hit,
            uncovered_lines=uncovered,
            hit_counts={ln: c for ln, c in hit_counts.items() if c > 0},
        )

    def get_branch_coverage(self):
        from .coverage import BranchCoverage

        branches: List[dict] = []
        total = 0
        hit = 0
        for site in self._branch_sites:
            for arm in site.arms:
                total += 1
                is_hit = arm.hit_count > 0
                if is_hit:
                    hit += 1
                branches.append(
                    {
                        "location": site.location,
                        "label": arm.label,
                        "hit": bool(is_hit),
                        "count": int(arm.hit_count),
                        "line_no": int(arm.line_no),
                        "site_id": site.site_id,
                        "kind": site.kind,
                    }
                )
        return BranchCoverage(total=total, hit=hit, branches=branches)


    # (helper removed; we execute against the original parsed always blocks)

