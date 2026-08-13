"""Lightweight RTL profile extraction before testbench generation.

Uses regex and balanced-parenthesis scanning — not a full SystemVerilog parser.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .analyzer import (
    Port,
    PortDirection,
    _detect_clock_port,
    _detect_reset_port,
    _extract_module_body,
    _extract_module_interface,
    _find_matching_paren,
    _read_param_value,
    _strip_comments,
    analyze_rtl,
)
from .always_model import extract_always_blocks


class InputKind(str, Enum):
    CLOCK = "clock"
    RESET = "reset"
    ENABLE = "enable"
    SELECT = "select"
    ADDRESS = "address"
    DATA_BUS = "data_bus"
    CONTROL = "control"


class DesignKind(str, Enum):
    COMBINATIONAL = "combinational"
    SEQUENTIAL = "sequential"


@dataclass
class ParameterInfo:
    name: str
    type_hint: str
    raw_default: str
    evaluated: Optional[int] = None


@dataclass
class PortInfo:
    name: str
    direction: str
    msb: int
    lsb: int
    width: int
    range_str: str
    is_scalar: bool
    is_packed_array: bool
    is_unpacked_array: bool
    packed_dims: List[str] = field(default_factory=list)
    input_kind: Optional[str] = None  # only for inputs

    @classmethod
    def from_port(cls, port: Port, input_kind: Optional[InputKind] = None) -> "PortInfo":
        packed_dims: List[str] = []
        if port.extra_ranges:
            packed_dims.append(port.extra_ranges)
        if not port.is_scalar or port.extra_ranges:
            hi, lo = max(port.msb, port.lsb), min(port.msb, port.lsb)
            packed_dims.append(f"[{hi}:{lo}]")
        # Dimensions parsed before the port name are packed (incl. [N][W] matrices).
        is_packed = len(packed_dims) >= 1
        is_unpacked = False
        return cls(
            name=port.name,
            direction=port.direction.value,
            msb=port.msb,
            lsb=port.lsb,
            width=port.width,
            range_str=port.range_str(),
            is_scalar=port.is_scalar,
            is_packed_array=is_packed,
            is_unpacked_array=is_unpacked,
            packed_dims=packed_dims,
            input_kind=input_kind.value if input_kind else None,
        )


@dataclass
class TypedefInfo:
    name: str
    kind: str  # "enum", "struct", "other"
    raw: str
    enum_values: List[str] = field(default_factory=list)


@dataclass
class CombinationalBlockInfo:
    index: int
    body: str
    assigned_signals: List[str] = field(default_factory=list)


@dataclass
class RtlProfile:
    module_name: str
    design_kind: str
    is_sequential: bool
    is_combinational: bool
    clock_port: Optional[str]
    reset_port: Optional[str]
    reset_active_low: bool
    has_always_ff: bool
    has_clocked_always: bool
    parameters: List[ParameterInfo]
    ports: List[PortInfo]
    internal_comb_signals: List[str]
    combinational_blocks: List[CombinationalBlockInfo]
    typedefs: List[TypedefInfo]
    enums: List[TypedefInfo]
    inferred_operation: Optional[str]
    unsupported_constructs: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_ENABLE_SUFFIXES = ("_en", "_enable")
_ENABLE_NAMES = frozenset({"en", "enable", "valid", "ce", "chip_enable"})
_SELECT_NAMES = frozenset({"sel", "select", "mux_sel", "selector", "choose"})
_ADDRESS_NAMES = frozenset({"addr", "address", "ptr", "pointer", "index", "idx"})
_ADDRESS_SUFFIXES = ("_addr", "_address", "_idx", "_index", "_ptr")
_DATA_NAMES = frozenset(
    {
        "data",
        "wdata",
        "rdata",
        "payload",
        "din",
        "dout",
        "bus",
        "req",
        "request",
        "grant",
        "weight",
        "weights",
        "mask",
        "vector",
    }
)
_CONTROL_NAMES = frozenset(
    {"go", "start", "stop", "flush", "clear", "halt", "run", "mode", "cfg", "config"}
)


def parse_rtl_profile(rtl: str, top_module: Optional[str] = None) -> RtlProfile:
    """Parse RTL and return a structured profile for testbench planning."""
    mod = analyze_rtl(rtl, top_module=top_module)
    clean = _strip_comments(rtl)
    body = _extract_module_body(clean, mod.name)
    header, param_ints = _extract_module_interface(clean, mod.name)
    if not header.strip():
        from .analyzer import _extract_header_ports

        header = _extract_header_ports(clean, mod.name)

    parameters = _extract_parameters(clean, mod.name, param_ints)
    typedefs, enums = _extract_typedefs_and_enums(body)
    comb_blocks = _extract_comb_block_info(body)
    internal_signals = _collect_internal_comb_signals(body, comb_blocks)

    clock = mod.clock_port
    reset = mod.reset_port
    port_infos = [
        _port_info(p, clock, reset, mod.reset_active_low) for p in mod.ports
    ]

    has_always_ff = bool(re.search(r"\balways_ff\b", body, re.IGNORECASE))
    has_clocked = bool(
        re.search(r"always\s*@\s*\([^)]*\b(?:posedge|negedge)\b", body, re.IGNORECASE)
    )
    is_seq = mod.is_sequential or has_always_ff or has_clocked or bool(clock)

    kind = DesignKind.SEQUENTIAL if is_seq else DesignKind.COMBINATIONAL

    return RtlProfile(
        module_name=mod.name,
        design_kind=kind.value,
        is_sequential=is_seq,
        is_combinational=not is_seq,
        clock_port=clock,
        reset_port=reset,
        reset_active_low=mod.reset_active_low,
        has_always_ff=has_always_ff,
        has_clocked_always=has_clocked,
        parameters=parameters,
        ports=port_infos,
        internal_comb_signals=sorted(internal_signals),
        combinational_blocks=comb_blocks,
        typedefs=typedefs,
        enums=enums,
        inferred_operation=mod.inferred_op,
        unsupported_constructs=list(mod.unsupported_constructs),
    )


def _port_info(
    port: Port,
    clock: Optional[str],
    reset: Optional[str],
    reset_active_low: bool,
) -> PortInfo:
    kind: Optional[InputKind] = None
    if port.direction == PortDirection.INPUT:
        kind = classify_input(port, clock, reset, reset_active_low)
    return PortInfo.from_port(port, kind)


def classify_input(
    port: Port,
    clock: Optional[str],
    reset: Optional[str],
    reset_active_low: bool,
) -> InputKind:
    """Heuristic classification of an input port."""
    del reset_active_low
    name = port.name
    nl = name.lower()

    if clock and name == clock:
        return InputKind.CLOCK
    if reset and name == reset:
        return InputKind.RESET

    if nl in {"clk", "clock", "ck"}:
        return InputKind.CLOCK
    if nl in {"rst", "reset", "rst_n", "resetn", "reset_n", "nrst"} or (
        nl.endswith("_n") and ("rst" in nl or "reset" in nl)
    ):
        return InputKind.RESET

    if nl in _ENABLE_NAMES or nl.endswith(_ENABLE_SUFFIXES):
        return InputKind.ENABLE

    if nl in _SELECT_NAMES or nl.endswith("_sel") or "select" in nl:
        return InputKind.SELECT

    if (
        nl in _ADDRESS_NAMES
        or nl.endswith(_ADDRESS_SUFFIXES)
        or nl in {"last_grant", "last_idx", "prev_grant"}
    ):
        return InputKind.ADDRESS

    if nl in _DATA_NAMES or port.width > 1:
        return InputKind.DATA_BUS

    if nl in _CONTROL_NAMES:
        return InputKind.CONTROL

    return InputKind.CONTROL if port.is_scalar else InputKind.DATA_BUS


def _extract_parameters(
    clean: str, module_name: str, evaluated: Dict[str, int]
) -> List[ParameterInfo]:
    m = re.search(rf"\bmodule\s+{re.escape(module_name)}\b", clean)
    if not m:
        return []
    idx = m.end()
    while idx < len(clean) and clean[idx].isspace():
        idx += 1
    if idx >= len(clean) or clean[idx] != "#":
        return _parameters_from_dict(evaluated)

    hash_end = _find_matching_paren(clean, idx + 1)
    param_text = clean[idx + 1 : hash_end]
    out: List[ParameterInfo] = []
    for m in re.finditer(
        r"(?:(localparam|parameter)\s+)?(?:(int|integer|logic|bit|byte)\s+)?(\w+)\s*=",
        param_text,
        re.IGNORECASE,
    ):
        kind = (m.group(1) or "parameter").lower()
        type_hint = (m.group(2) or "int").lower()
        name = m.group(3)
        raw = _read_param_value(param_text, m.end()).strip()
        out.append(
            ParameterInfo(
                name=name,
                type_hint=type_hint,
                raw_default=raw,
                evaluated=evaluated.get(name),
            )
        )
    return out or _parameters_from_dict(evaluated)


def _parameters_from_dict(evaluated: Dict[str, int]) -> List[ParameterInfo]:
    return [
        ParameterInfo(name=k, type_hint="int", raw_default=str(v), evaluated=v)
        for k, v in evaluated.items()
    ]


def _extract_typedefs_and_enums(body: str) -> tuple[List[TypedefInfo], List[TypedefInfo]]:
    typedefs: List[TypedefInfo] = []
    enums: List[TypedefInfo] = []

    for m in re.finditer(
        r"typedef\s+(enum\s+(?:logic|reg|bit)?\s*(?:\[[^\]]+\])?\s*\{([^}]*)\}\s*(\w+)|"
        r"(struct\s+(?:packed\s+)?\{[^}]*\}\s*(\w+))|"
        r"(\w+(?:\s*\[[^\]]+\])?)\s+(\w+)\s*;)",
        body,
        re.DOTALL | re.IGNORECASE,
    ):
        if m.group(1) is not None or m.group(2):
            enum_body = m.group(2) or ""
            enum_name = m.group(3) or ""
            values = _parse_enum_values(enum_body)
            info = TypedefInfo(
                name=enum_name,
                kind="enum",
                raw=m.group(0).strip(),
                enum_values=values,
            )
            typedefs.append(info)
            enums.append(info)
        elif m.group(4):
            info = TypedefInfo(
                name=m.group(4),
                kind="struct",
                raw=m.group(0).strip(),
            )
            typedefs.append(info)
        elif m.group(5) and m.group(6):
            info = TypedefInfo(
                name=m.group(6),
                kind="other",
                raw=m.group(0).strip(),
            )
            typedefs.append(info)

    for m in re.finditer(
        r"(?<!typedef\s)(?<!typedef)enum\s+(?:logic|reg|bit)?\s*(?:\[[^\]]+\])?\s*\{([^}]*)\}\s*(\w+)\s*;",
        body,
        re.IGNORECASE,
    ):
        values = _parse_enum_values(m.group(1))
        info = TypedefInfo(
            name=m.group(2),
            kind="enum",
            raw=m.group(0).strip(),
            enum_values=values,
        )
        typedefs.append(info)
        enums.append(info)

    return typedefs, enums


def _parse_enum_values(enum_body: str) -> List[str]:
    values: List[str] = []
    for part in enum_body.split(","):
        part = part.strip()
        if not part:
            continue
        name = re.split(r"\s*=\s*", part)[0].strip()
        if name and re.fullmatch(r"\w+", name):
            values.append(name)
    return values


def _extract_comb_block_info(body: str) -> List[CombinationalBlockInfo]:
    blocks = extract_always_blocks(body)
    out: List[CombinationalBlockInfo] = []
    for i, block in enumerate(blocks):
        assigned = _signals_assigned_in_block(block)
        out.append(
            CombinationalBlockInfo(
                index=i,
                body=block,
                assigned_signals=sorted(assigned),
            )
        )
    return out


def _signals_assigned_in_block(block: str) -> set[str]:
    assigned: set[str] = set()
    skip = {"if", "for", "case", "unique", "priority"}
    for m in re.finditer(r"\b([A-Za-z_]\w*)(?:\s*\[[^\]]*\])?\s*=", block):
        lhs = m.group(1)
        if lhs not in skip:
            assigned.add(lhs)
    return assigned


def _collect_internal_comb_signals(
    body: str, comb_blocks: List[CombinationalBlockInfo]
) -> set[str]:
    declared = _declared_internal_signals(body)
    from_blocks: set[str] = set()
    for blk in comb_blocks:
        from_blocks.update(blk.assigned_signals)
    port_names = _port_names_in_body_header(body)
    # Drop common loop indices
    from_blocks -= {"i", "j", "k"}
    return (declared & from_blocks) - port_names


def _port_names_in_body_header(body: str) -> set[str]:
    cut = re.search(r"\b(always|assign|endmodule|generate)\b", body, re.IGNORECASE)
    region = body[: cut.start()] if cut else body
    names: set[str] = set()
    for m in re.finditer(
        r"(?:input|output|inout)\s+(?:[\w\[\]:.\s-]+\s+)?(\w+)\s*[,;]",
        region,
        re.IGNORECASE,
    ):
        names.add(m.group(1))
    return names


def _declared_internal_signals(body: str) -> set[str]:
    """Signals declared as logic/reg/wire inside the module (not in port list)."""
    names: set[str] = set()
    cut = re.search(r"\bendmodule\b", body, re.IGNORECASE)
    region = body[: cut.start()] if cut else body

    for m in re.finditer(
        r"(?:logic|reg|wire|bit)\s+((?:\[[^\]]+\]\s*)*)([\w,\s]+)\s*;",
        region,
        re.IGNORECASE,
    ):
        name_blob = m.group(2)
        for raw_name in name_blob.split(","):
            name = raw_name.strip()
            if re.fullmatch(r"\w+", name):
                names.add(name)
    return names
