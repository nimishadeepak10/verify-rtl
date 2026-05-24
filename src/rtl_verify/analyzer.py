"""Parse Verilog/SystemVerilog RTL to extract module interface and hints."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set, Tuple

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

    @property
    def width(self) -> int:
        return abs(self.msb - self.lsb) + 1

    @property
    def is_scalar(self) -> bool:
        return self.width == 1

    def range_str(self) -> str:
        if self.is_scalar:
            return ""
        hi, lo = max(self.msb, self.lsb), min(self.msb, self.lsb)
        return f"[{hi}:{lo}]"


@dataclass
class RtlModule:
    name: str
    ports: List[Port] = field(default_factory=list)
    source: str = ""
    inferred_op: Optional[str] = None  # e.g. "add", "and", "xor"
    clock_port: Optional[str] = None
    reset_port: Optional[str] = None
    reset_active_low: bool = False
    is_sequential: bool = False
    state_reg: Optional[str] = None
    states: List[str] = field(default_factory=list)

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

    mod_match = re.search(
        rf"\bmodule\s+{re.escape(target_name)}\s*(?:#\s*\([^)]*\)\s*)?\((.*?)\)\s*;",
        clean,
        re.DOTALL,
    )
    if not mod_match:
        mod_match = re.search(
            rf"\bmodule\s+{re.escape(target_name)}\b(.*?)\bendmodule\b",
            clean,
            re.DOTALL,
        )
        if not mod_match:
            raise ValueError(f"Module '{target_name}' not found")

    header = mod_match.group(1) if mod_match.lastindex else ""
    if not header.strip():
        header = _extract_header_ports(clean, target_name)

    body = _extract_module_body(clean, target_name)
    ports = _parse_port_list(header)
    clock_port = _detect_clock_port(ports)
    reset_port, reset_active_low = _detect_reset_port(ports)
    is_sequential = _detect_sequential(body)
    state_reg, states = _detect_fsm(body, clock_port)
    inferred = _infer_operation(clean, ports)

    return RtlModule(
        name=target_name,
        ports=ports,
        source=rtl,
        inferred_op=inferred,
        clock_port=clock_port,
        reset_port=reset_port,
        reset_active_low=reset_active_low,
        is_sequential=is_sequential,
        state_reg=state_reg,
        states=states,
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
    chunk = clean[m.end() : m.end() + 2000]
    paren = re.search(r"\((.*?)\)\s*;", chunk, re.DOTALL)
    return paren.group(1) if paren else ""


def _parse_port_list(header: str) -> List[Port]:
    ports: List[Port] = []
    header = header.replace("\n", " ")
    chunks = re.split(r",\s*(?=(?:input|output|inout)\b)", header)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(
            r"(input|output|inout)\s+(?:(?:wire|reg|logic)\s+)?(?:\[(\d+)\s*:\s*(\d+)\]\s+)?(\w+)",
            chunk,
            re.IGNORECASE,
        )
        if not m:
            continue
        direction = PortDirection(m.group(1).lower())
        if m.group(2) is not None:
            msb, lsb = _parse_width(m.group(2), m.group(3))
        else:
            msb, lsb = 0, 0
        ports.append(Port(name=m.group(4), direction=direction, msb=msb, lsb=lsb))
    return ports


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


def _detect_fsm(body: str, clock_port: Optional[str]) -> Tuple[Optional[str], List[str]]:
    """Find state register and case labels in a clocked FSM."""
    case_map: dict[str, List[str]] = {}
    for m in re.finditer(
        r"case\s*\(\s*(\w+)\s*\)(.*?)endcase",
        body,
        re.DOTALL | re.IGNORECASE,
    ):
        var = m.group(1)
        labels = _extract_case_labels(m.group(2))
        if labels:
            case_map[var] = labels

    for var, labels in case_map.items():
        if not re.search(rf"\b{re.escape(var)}\s*<=", body):
            continue
        if _assigned_in_clocked_block(body, var, clock_port):
            return var, labels
    return None, []


def _extract_case_labels(case_body: str) -> List[str]:
    labels: List[str] = []
    for m in re.finditer(r"^\s*([A-Za-z_]\w*)\s*:", case_body, re.MULTILINE):
        lab = m.group(1)
        if lab.lower() not in _CASE_KEYWORDS:
            labels.append(lab)
    return labels


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


def _infer_operation(clean: str, ports: List[Port]) -> Optional[str]:
    if re.search(r"assign\s+\w+\s*=\s*\w+\s*\+\s*\w+", clean):
        return "add"
    if re.search(r"assign\s+\w+\s*=\s*\w+\s*&\s*\w+", clean):
        return "and"
    if re.search(r"assign\s+\w+\s*=\s*\w+\s*\|\s*\w+", clean):
        return "or"
    if re.search(r"assign\s+\w+\s*=\s*\w+\s*-\s*\w+", clean):
        return "sub"
    if re.search(r"assign\s+\w+\s*=\s*~\s*\w+", clean):
        return "not"
    ins = [p for p in ports if p.direction == PortDirection.INPUT]
    outs = [p for p in ports if p.direction == PortDirection.OUTPUT]
    if len(ins) == 2 and len(outs) == 1:
        return "binary_op"
    return None
