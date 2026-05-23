"""Parse Verilog/SystemVerilog RTL to extract module interface and hints."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


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

    @property
    def inputs(self) -> List[Port]:
        return [p for p in self.ports if p.direction == PortDirection.INPUT]

    @property
    def outputs(self) -> List[Port]:
        return [p for p in self.ports if p.direction == PortDirection.OUTPUT]


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

    body_start = mod_match.end() if mod_match.lastindex else mod_match.start(1)
    header = mod_match.group(1) if mod_match.lastindex else ""
    if not header.strip():
        header = _extract_header_ports(clean, target_name)

    ports = _parse_port_list(header)
    inferred = _infer_operation(clean, ports)
    return RtlModule(name=target_name, ports=ports, source=rtl, inferred_op=inferred)


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


def _infer_operation(clean: str, ports: List[Port]) -> Optional[str]:
    if re.search(r"assign\s+\w+\s*=\s*\w+\s*\+\s*\w+", clean):
        return "add"
    if re.search(r"assign\s+\w+\s*=\s*\w+\s*&\s*\w+", clean):
        return "and"
    if re.search(r"assign\s+\w+\s*=\s*\w+\s*\^\s*\w+", clean):
        return "xor"
    ins = [p for p in ports if p.direction == PortDirection.INPUT]
    outs = [p for p in ports if p.direction == PortDirection.OUTPUT]
    if len(ins) == 2 and len(outs) == 1:
        return "binary_op"
    return None
