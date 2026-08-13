"""Parse Verilog/SystemVerilog RTL to extract module interface and hints."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

_CLOCK_NAMES = frozenset({"clk", "clock", "ck"})
_RESET_ACTIVE_LOW = frozenset({"rst_n", "resetn", "reset_n", "nrst"})
_RESET_ACTIVE_HIGH = frozenset({"rst", "reset"})
_CASE_KEYWORDS = frozenset({"default", "unique", "priority", "endcase", "case", "inside"})


class PortDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    INOUT = "inout"


@dataclass
class Port:
    name: str
    direction: PortDirection
    msb: int = 0
    lsb: int = 0
    extra_ranges: str = ""  # outer packed/unpacked dims, e.g. "[7:0]" before inner [3:0]

    @property
    def width(self) -> int:
        inner = abs(self.msb - self.lsb) + 1
        if not self.extra_ranges:
            return inner
        total = inner
        for m in re.finditer(r"\[(\d+)\s*:\s*(\d+)\]", self.extra_ranges):
            total *= abs(int(m.group(1)) - int(m.group(2))) + 1
        return total

    @property
    def is_scalar(self) -> bool:
        return not self.extra_ranges and self.msb == self.lsb == 0

    @property
    def is_unpacked_array(self) -> bool:
        return bool(self.extra_ranges)

    def range_str(self) -> str:
        inner = ""
        if not self.is_scalar or self.extra_ranges:
            hi, lo = max(self.msb, self.lsb), min(self.msb, self.lsb)
            inner = f"[{hi}:{lo}]"
        return f"{self.extra_ranges}{inner}"


@dataclass
class RtlModule:
    name: str
    ports: List[Port] = field(default_factory=list)
    source: str = ""
    inferred_op: Optional[str] = None  # e.g. "add", "sum_with_carry", "case_dispatch"
    unsupported_constructs: List[str] = field(default_factory=list)
    clock_port: Optional[str] = None
    reset_port: Optional[str] = None
    reset_active_low: bool = False
    is_sequential: bool = False
    state_reg: Optional[str] = None
    states: List[str] = field(default_factory=list)
    fsm_value_map: dict[str, str] = field(default_factory=dict)  # literal -> state name
    fsm_transitions: list[tuple[str, str]] = field(default_factory=list)  # (from,to) pairs (best-effort)
    combinational_blocks: List[str] = field(default_factory=list)  # always @(*) bodies

    @property
    def inputs(self) -> List[Port]:
        return [p for p in self.ports if p.direction == PortDirection.INPUT]

    @property
    def outputs(self) -> List[Port]:
        return [p for p in self.ports if p.direction == PortDirection.OUTPUT]

    @property
    def data_inputs(self) -> List[Port]:
        """Input ports excluding clock and reset."""
        skip: Set[str] = set()
        if self.clock_port:
            skip.add(self.clock_port)
        if self.reset_port:
            skip.add(self.reset_port)
        return [p for p in self.inputs if p.name not in skip]


def _parse_width(msb: str, lsb: str) -> tuple[int, int]:
    return int(msb.strip()), int(lsb.strip())


def _find_matching_paren(text: str, open_idx: int) -> int:
    if open_idx >= len(text) or text[open_idx] != "(":
        raise ValueError("expected '('")
    depth = 0
    for i in range(open_idx, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced parentheses")


def _skip_ws(text: str, idx: int) -> int:
    while idx < len(text) and text[idx].isspace():
        idx += 1
    return idx


def _parse_parameter_defaults(param_text: str) -> Dict[str, int]:
    """Best-effort evaluation of parameter defaults (supports int and $clog2)."""
    params: Dict[str, int] = {}
    for m in re.finditer(r"parameter\s+(?:\w+\s+)?(\w+)\s*=", param_text, re.IGNORECASE):
        name = m.group(1)
        val_start = m.end()
        val_text = _read_param_value(param_text, val_start)
        val = _eval_param_expr(val_text.strip(), params)
        if val is not None:
            params[name] = val
    return params


def _read_param_value(text: str, start: int) -> str:
    """Read a parameter RHS up to the next top-level comma or closing paren."""
    i = start
    depth = 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    return text[start:i].strip()


def _eval_param_expr(expr: str, params: Dict[str, int]) -> Optional[int]:
    expr = expr.strip()
    if not expr:
        return None
    m = re.fullmatch(r"\$clog2\s*\(\s*(\w+)\s*\)", expr)
    if m:
        base = params.get(m.group(1))
        if base is None or base <= 0:
            return None
        return int(math.ceil(math.log2(base)))
    if re.fullmatch(r"-?\d+", expr):
        return int(expr)
    if expr in params:
        return params[expr]
    m = re.fullmatch(r"(\w+)\s*-\s*(\d+)", expr)
    if m and m.group(1) in params:
        return params[m.group(1)] - int(m.group(2))
    m = re.fullmatch(r"(\w+)\s*\+\s*(\d+)", expr)
    if m and m.group(1) in params:
        return params[m.group(1)] + int(m.group(2))
    return None


def _eval_range_bounds(inner: str, params: Dict[str, int]) -> Optional[tuple[int, int]]:
    inner = inner.strip()
    m = re.fullmatch(r"(\d+)\s*:\s*(\d+)", inner)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"(\w+)\s*-\s*(\d+)\s*:\s*(\d+)", inner)
    if m and m.group(1) in params:
        hi = params[m.group(1)] - int(m.group(2))
        lo = int(m.group(3))
        return hi, lo
    m = re.fullmatch(r"(\w+)\s*-\s*(\d+)\s*:\s*(\w+)\s*-\s*(\d+)", inner)
    if m and m.group(1) in params and m.group(3) in params:
        hi = params[m.group(1)] - int(m.group(2))
        lo = params[m.group(3)] - int(m.group(4))
        return hi, lo
    return None


def _extract_module_interface(
    clean: str, target_name: str
) -> tuple[str, Dict[str, int]]:
    """Return (port-list text, parameter defaults) for a module header."""
    m = re.search(rf"\bmodule\s+{re.escape(target_name)}\b", clean)
    if not m:
        return "", {}
    idx = _skip_ws(clean, m.end())
    params: Dict[str, int] = {}
    if idx < len(clean) and clean[idx] == "#":
        hash_end = _find_matching_paren(clean, idx + 1)
        params = _parse_parameter_defaults(clean[idx + 1 : hash_end])
        idx = _skip_ws(clean, hash_end + 1)
    if idx >= len(clean) or clean[idx] != "(":
        return _extract_header_ports(clean, target_name), params
    port_end = _find_matching_paren(clean, idx)
    return clean[idx + 1 : port_end], params


def _strip_comments(rtl: str) -> str:
    rtl = re.sub(r"//.*?$", "", rtl, flags=re.MULTILINE)
    rtl = re.sub(r"/\*.*?\*/", "", rtl, flags=re.DOTALL)
    return rtl


def analyze_rtl(rtl: str, top_module: Optional[str] = None) -> RtlModule:
    """Extract the first (or named) module and its ports from RTL text."""
    clean = _strip_comments(rtl)
    modules = list(re.finditer(r"\bmodule\s+(\w+)\s*[#(]?", clean))
    if not modules:
        raise ValueError("No module declaration found in RTL")

    target_name = top_module
    if not target_name:
        target_name = modules[0].group(1)

    header, param_defaults = _extract_module_interface(clean, target_name)
    if not header.strip():
        header = _extract_header_ports(clean, target_name)

    body = _extract_module_body(clean, target_name)
    ports = _parse_port_list(header, param_defaults)
    if not ports and header.strip():
        ports = _parse_name_only_header(header)
    body_ports = _parse_body_port_declarations(body, param_defaults)
    if body_ports:
        ports = _merge_ports_by_name(ports, body_ports) if ports else body_ports
    if not ports:
        raise ValueError(
            f"Could not extract ports for module '{target_name}'. "
            "Use ANSI-style port declarations (input/output/inout) in the header or body."
        )
    clock_port = _detect_clock_port(ports)
    reset_port, reset_active_low = _detect_reset_port(ports)
    is_sequential = _detect_sequential(body)
    state_reg, states, fsm_value_map, fsm_transitions = _detect_fsm(body, clock_port)
    comb_blocks = _detect_combinational_blocks(body)
    from .unsupported_scan import scan_rtl_for_unsupported

    scan = scan_rtl_for_unsupported(rtl)
    unsupported_msgs = [u.message() for u in scan.unsupported_constructs]
    inferred = _infer_operation(clean, ports, body, rtl_source=clean)

    return RtlModule(
        name=target_name,
        ports=ports,
        source=rtl,
        inferred_op=inferred,
        unsupported_constructs=unsupported_msgs,
        clock_port=clock_port,
        reset_port=reset_port,
        reset_active_low=reset_active_low,
        is_sequential=is_sequential,
        state_reg=state_reg,
        states=states,
        fsm_value_map=fsm_value_map,
        fsm_transitions=fsm_transitions,
        combinational_blocks=comb_blocks,
    )


def _extract_module_body(clean: str, name: str) -> str:
    m = re.search(
        rf"\bmodule\s+{re.escape(name)}\b.*?\bendmodule\b",
        clean,
        re.DOTALL | re.IGNORECASE,
    )
    return m.group(0) if m else clean


def _extract_header_ports(clean: str, name: str) -> str:
    m = re.search(rf"\bmodule\s+{re.escape(name)}\b", clean)
    if not m:
        return ""
    idx = _skip_ws(clean, m.end())
    if idx < len(clean) and clean[idx] == "#":
        idx = _skip_ws(clean, _find_matching_paren(clean, idx + 1) + 1)
    if idx >= len(clean) or clean[idx] != "(":
        return ""
    port_end = _find_matching_paren(clean, idx)
    return clean[idx + 1 : port_end]


def _parse_port_list(header: str, params: Optional[Dict[str, int]] = None) -> List[Port]:
    params = params or {}
    ports: List[Port] = []
    header = re.sub(r"\s+", " ", header.strip())
    chunks = re.split(r",\s*(?=(?:input|output|inout)\b)", header)
    for chunk in chunks:
        chunk = chunk.strip().rstrip(",").strip()
        if not chunk:
            continue
        m = re.match(
            r"(input|output|inout)\s+"
            r"(?:(?:wire|reg|logic|signed|unsigned)\s+)*"
            r"(.*)",
            chunk,
            re.IGNORECASE,
        )
        if not m:
            continue
        direction = PortDirection(m.group(1).lower())
        rest = m.group(2).strip().rstrip(",").strip()
        range_parts: List[str] = []
        while rest.startswith("["):
            close = rest.find("]")
            if close < 0:
                break
            range_parts.append(rest[: close + 1])
            rest = rest[close + 1 :].strip()
        name_m = re.match(r"([\w\s,]+)\s*$", rest)
        if not name_m:
            continue
        names = [n.strip() for n in name_m.group(1).split(",") if n.strip()]
        if not names:
            continue
        evaluated_ranges: List[str] = []
        for rp in range_parts:
            bounds = _eval_range_bounds(rp[1:-1], params)
            if bounds is not None:
                hi, lo = bounds
                evaluated_ranges.append(f"[{hi}:{lo}]")
            else:
                evaluated_ranges.append(rp)
        extra_ranges = ""
        msb, lsb = 0, 0
        if len(evaluated_ranges) == 0:
            pass
        elif len(evaluated_ranges) == 1:
            bounds = _eval_range_bounds(evaluated_ranges[0][1:-1], params)
            if bounds is not None:
                msb, lsb = bounds
            else:
                m_lit = re.search(r"\[(\d+)\s*:\s*(\d+)\]", evaluated_ranges[0])
                if m_lit:
                    msb, lsb = int(m_lit.group(1)), int(m_lit.group(2))
        else:
            inner_bounds = _eval_range_bounds(evaluated_ranges[-1][1:-1], params)
            if inner_bounds is not None:
                msb, lsb = inner_bounds
            extra_ranges = "".join(evaluated_ranges[:-1])
        for name in names:
            if not re.fullmatch(r"\w+", name):
                continue
            ports.append(
                Port(
                    name=name,
                    direction=direction,
                    msb=msb,
                    lsb=lsb,
                    extra_ranges=extra_ranges,
                )
            )
    return ports


def _parse_name_only_header(header: str) -> List[Port]:
    """Non-ANSI header: module foo (a, b, y); — directions come from body."""
    names = [n.strip() for n in header.split(",") if n.strip()]
    out: List[Port] = []
    for name in names:
        if re.fullmatch(r"\w+", name):
            out.append(Port(name=name, direction=PortDirection.INOUT))
    return out


def _parse_body_port_declarations(body: str, params: Dict[str, int]) -> List[Port]:
    """input/output/inout declarations inside the module body."""
    cut = re.search(r"\b(always|assign|endmodule|generate)\b", body, re.IGNORECASE)
    region = body[: cut.start()] if cut else body
    ports: List[Port] = []
    for stmt in re.split(r";", region):
        stmt = stmt.strip()
        if not stmt:
            continue
        if re.match(r"(input|output|inout)\b", stmt, re.IGNORECASE):
            ports.extend(_parse_port_list(stmt, params))
    return ports


def _merge_ports_by_name(primary: List[Port], overrides: List[Port]) -> List[Port]:
    by_name: Dict[str, Port] = {p.name: p for p in primary}
    for p in overrides:
        by_name[p.name] = p
    return list(by_name.values())


def _detect_clock_port(ports: List[Port]) -> Optional[str]:
    for p in ports:
        if p.direction == PortDirection.INPUT and p.name.lower() in _CLOCK_NAMES:
            return p.name
    return None


def _detect_reset_port(ports: List[Port]) -> Tuple[Optional[str], bool]:
    for p in ports:
        if p.direction != PortDirection.INPUT:
            continue
        nl = p.name.lower()
        if nl in _RESET_ACTIVE_LOW:
            return p.name, True
        if nl.endswith("_n") and ("rst" in nl or "reset" in nl):
            return p.name, True
        if nl.startswith("n") and nl[1:] in ("rst", "reset"):
            return p.name, True
        if nl in _RESET_ACTIVE_HIGH:
            return p.name, False
    return None, False


def _detect_sequential(body: str) -> bool:
    if re.search(r"always\s*@\s*\([^)]*\bposedge\b", body, re.IGNORECASE):
        return True
    if re.search(r"always\s*@\s*\([^)]*\bnegedge\b", body, re.IGNORECASE):
        return True
    if re.search(r"\balways_ff\b", body, re.IGNORECASE):
        return True
    if re.search(r"\b\w+\s*<=", body):
        return True
    return False


def _detect_fsm(
    body: str, clock_port: Optional[str]
) -> Tuple[Optional[str], List[str], dict[str, str], list[tuple[str, str]]]:
    """Find state register + best-effort state labels and transitions in a clocked FSM."""
    case_map: dict[str, Tuple[List[str], dict[str, str], list[tuple[str, str]]]] = {}
    for m in re.finditer(
        r"case\s*\(\s*(\w+)\s*\)(.*?)endcase",
        body,
        re.DOTALL | re.IGNORECASE,
    ):
        var = m.group(1)
        labels, value_map, transitions = _extract_case_labels_and_transitions(m.group(2))
        if labels:
            case_map[var] = (labels, value_map, transitions)

    for var, tup in case_map.items():
        labels, value_map, transitions = tup
        if not re.search(rf"\b{re.escape(var)}\s*<=", body):
            continue
        if _assigned_in_clocked_block(body, var, clock_port):
            return var, labels, value_map, transitions
    return None, [], {}, []


def _extract_case_labels_and_transitions(
    case_body: str,
) -> Tuple[List[str], dict[str, str], list[tuple[str, str]]]:
    """
    Extract case arm labels (state names) and a best-effort literal->name map and transitions.
    Supported arm forms:
    - IDLE: begin ... state <= ACTIVE; ... end
    - 3'b000: begin ... end   (no name)
    """
    labels: List[str] = []
    value_map: dict[str, str] = {}
    transitions: list[tuple[str, str]] = []

    # Capture each arm block as text for simple transition extraction.
    arm_re = re.compile(
        r"^\s*([A-Za-z_]\w*|default|\d+'\s*[bhdBHD]\s*[0-9a-fA-F_]+)\s*:\s*(begin)?(.*?)(?=^\s*(?:[A-Za-z_]\w*|default|\d+'\s*[bhdBHD]\s*[0-9a-fA-F_]+)\s*:|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    for m in arm_re.finditer(case_body):
        lab = m.group(1).strip()
        if lab.lower() in _CASE_KEYWORDS:
            continue
        labels.append(lab)
        # heuristic: if label is a name, keep it as-is; if it's a literal, map literal->literal
        if re.match(r"^\d+'\s*[bhdBHD]\s*", lab):
            value_map[re.sub(r"\s+", "", lab)] = lab
        else:
            value_map[lab] = lab

        arm_text = m.group(3) or ""
        # best-effort transition extraction: find "state <= NEXT" with NEXT as identifier
        for t in re.finditer(r"\b(\w+)\s*<=\s*(\w+)\s*;", arm_text):
            from_state = lab
            to_state = t.group(2)
            if to_state and from_state and to_state != from_state:
                transitions.append((from_state, to_state))

    return labels, value_map, transitions


def _assigned_in_clocked_block(body: str, signal: str, clock_port: Optional[str]) -> bool:
    blocks = re.split(r"(?=\balways(?:_ff)?\s*@)", body, flags=re.IGNORECASE)
    for block in blocks:
        if not re.match(r"\balways", block, re.IGNORECASE):
            continue
        is_clocked = bool(re.search(r"\balways_ff\b", block, re.IGNORECASE))
        if clock_port:
            clk = re.escape(clock_port)
            is_clocked = is_clocked or bool(
                re.search(rf"\bposedge\s+{clk}\b", block, re.IGNORECASE)
                or re.search(rf"\bnegedge\s+{clk}\b", block, re.IGNORECASE)
            )
        if not is_clocked:
            continue
        if re.search(rf"\b{re.escape(signal)}\s*<=", block):
            return True
    return False


def _detect_combinational_blocks(body: str) -> List[str]:
    from .always_model import extract_always_blocks

    return extract_always_blocks(body)


def _detect_case_selector(text: str) -> Optional[str]:
    m = re.search(r"case\s*\(\s*(\w+)\s*\)", text, re.IGNORECASE)
    return m.group(1) if m else None


def _detect_case_dispatch_labels(text: str) -> List[str]:
    labels: List[str] = []
    for m in re.finditer(r"case\s*\([^)]+\)(.*?)endcase", text, re.DOTALL | re.IGNORECASE):
        for lab in re.finditer(r"^\s*(\w+)\s*:", m.group(1), re.MULTILINE):
            name = lab.group(1)
            if name.lower() not in _CASE_KEYWORDS:
                labels.append(name)
    return labels


def _infer_operation(
    clean: str, ports: List[Port], body: str = "", rtl_source: str = ""
) -> Optional[str]:
    from .combinational_model import can_evaluate_combinational

    src = body or clean
    patterns: List[str] = []

    m_concat = re.search(
        r"assign\s*\{([^}]+)\}\s*=\s*([^;]+)\s*;",
        src,
        re.IGNORECASE | re.DOTALL,
    )
    if m_concat and "+" in m_concat.group(2):
        lhs = m_concat.group(1).strip()
        rhs = re.sub(r"\s+", "", m_concat.group(2).strip())
        return f"sum_with_carry: {{{lhs}}} = {rhs}"

    if re.search(r"casez\s*\(", src, re.IGNORECASE):
        sel_m = re.search(r"casez\s*\(\s*(\w+)\s*\)", src, re.IGNORECASE)
        sel = sel_m.group(1) if sel_m else "sel"
        return f"casez_dispatch on {sel}"

    if re.search(r"casex\s*\(", src, re.IGNORECASE):
        sel_m = re.search(r"casex\s*\(\s*(\w+)\s*\)", src, re.IGNORECASE)
        sel = sel_m.group(1) if sel_m else "sel"
        return f"casex_dispatch on {sel}"

    if re.search(r"always\s*@\s*\(\s*\*\s*\)", src, re.IGNORECASE):
        if re.search(r"case\s*\(", src, re.IGNORECASE):
            sel = _detect_case_selector(src)
            from .always_model import infer_case_arm_ops

            case_m = re.search(
                r"case\s*\([^)]+\)(.*?)endcase", src, re.DOTALL | re.IGNORECASE
            )
            ops = infer_case_arm_ops(case_m.group(1)) if case_m else []
            if sel and ops:
                return f"case_dispatch on {sel} → {{{', '.join(ops)}}}"
            return "case_dispatch"
        if re.search(r"\bif\s*\(", src, re.IGNORECASE):
            sel_m = re.search(r"if\s*\(\s*\w+\s*==[^)]+\)", src)
            if sel_m:
                return "if_chain on sel"
            return "if_chain"

    if re.search(r"\$signed\s*\(", src, re.IGNORECASE):
        patterns.append("signed_arithmetic")

    red_ops = []
    for op, name in (("^", "xor"), ("&", "and"), ("|", "or")):
        if re.search(rf"assign\s+\w+\s*=\s*~?{re.escape(op)}\w+", src):
            red_ops.append(f"reduction_{name}")
    if red_ops:
        patterns.extend(red_ops)

    if re.search(r"assign\s+\w+\s*=\s*[^;]+\?[^;]+:[^;]+;", src):
        m = re.search(r"assign\s+(\w+)\s*=\s*([^;]+);", src)
        if m:
            rhs = m.group(2).strip()
            if "?" in rhs:
                return f"conditional: {rhs[:60]}"

    m = re.search(r"assign\s+(\w+)\s*=\s*(\w+)\s*\+\s*(\w+)\s*;", clean)
    if m:
        return f"add: {m.group(1)} = {m.group(2)} + {m.group(3)}"

    m_add = re.search(r"assign\s+(\w+)\s*=\s*(.+)\s*;", src, re.DOTALL)
    if m_add:
        lhs, rhs = m_add.group(1), m_add.group(2).strip()
        rhs_compact = re.sub(r"\s+", "", rhs)
        plus_count = rhs_compact.count("+")
        if plus_count >= 2:
            return f"n_input_add: {lhs} = {rhs_compact}"
        if "&" in rhs_compact and rhs_compact.count("&") >= 2:
            return "n_input_and"
        if "|" in rhs_compact and rhs_compact.count("|") >= 2:
            return "n_input_or"

    if len(patterns) > 1:
        return "multi_pattern: " + ", ".join(patterns)
    if patterns:
        return patterns[0]

    if rtl_source:
        try:
            mod_tmp = RtlModule(name="_", ports=ports, source=rtl_source)
            if can_evaluate_combinational(rtl_source, mod_tmp):
                return "combinational_golden"
        except Exception:
            pass

    ins = [p for p in ports if p.direction == PortDirection.INPUT]
    outs = [p for p in ports if p.direction == PortDirection.OUTPUT]
    if len(ins) == 2 and len(outs) == 1:
        return "binary_op"
    return None
