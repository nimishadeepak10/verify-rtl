"""VCD parsing: text trace summary, JSON for visual viewer, and HTML preview."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .analyzer import PortDirection, RtlModule

_TB_NAME_HINTS = frozenset(
    {"clk", "clock", "reset", "rst", "rst_n", "resetn", "pass_cnt", "fail_cnt", "zero"}
)
_TB_SUFFIXES = ("_cnt", "_count")


def vcd_to_text(vcd_path: Path, max_samples: int = 64) -> str:
    """Produce human-readable signal samples from a VCD file."""
    if not vcd_path.exists():
        return "No VCD file generated."

    lines = vcd_path.read_text(encoding="utf-8", errors="replace").splitlines()
    id_to_name: Dict[str, str] = {}
    widths: Dict[str, int] = {}
    values: Dict[str, str] = {}
    samples: List[Tuple[int, Dict[str, str]]] = []
    current_time = 0

    for line in lines:
        line = line.strip()
        if line.startswith("$var"):
            # $var wire 3 ! a [1:0] $end  (variants exist)
            parts = line.split()
            if len(parts) >= 5:
                sym = parts[3]
                name = parts[4]
                id_to_name[sym] = name
                try:
                    widths[name] = int(parts[2])
                except ValueError:
                    widths[name] = 1
        elif line.startswith("#"):
            try:
                current_time = int(line[1:].split()[0])
            except ValueError:
                continue
        elif line and line[0] in "bBr":
            # b1010 !  or 1!  or r1.0 x
            if line[0] == "b":
                m = re.match(r"b([01xzXZ]+)\s*(\S+)", line)
                if m:
                    val, sym = m.group(1), m.group(2)
                    name = id_to_name.get(sym, sym)
                    values[name] = f"b{val}"
            else:
                m = re.match(r"([01xzXZ]+)\s*(\S+)", line[1:])
                if m:
                    val, sym = m.group(1), m.group(2)
                    name = id_to_name.get(sym, sym)
                    values[name] = val
            if values:
                samples.append((current_time, dict(values)))
                if len(samples) > max_samples * 4:
                    samples = samples[-max_samples * 2 :]

    if not samples:
        return "VCD parsed but no value changes found."

    out = ["=== WAVEFORM (text) ===", f"Signals: {', '.join(sorted(set(id_to_name.values())))}", ""]
    last_t = -1
    shown = 0
    for t, snap in samples:
        if t == last_t:
            continue
        last_t = t
        parts = [f"t={t}ns"] + [f"{k}={v}" for k, v in sorted(snap.items())]
        out.append("  ".join(parts))
        shown += 1
        if shown >= max_samples:
            out.append(f"... ({len(samples) - max_samples} more transitions truncated)")
            break
    return "\n".join(out)


def vcd_to_html(vcd_path: Path) -> str:
    """Embed VCD as downloadable file + ASCII preview for browser UI."""
    text = vcd_to_text(vcd_path, max_samples=32)
    escaped = text.replace("&", "&amp;").replace("<", "&lt;")
    return f"""<section class="waveform">
<h3>Waveform</h3>
<p>Download <code>sim.vcd</code> and open in GTKWave, Surfer, or ModelSim.</p>
<pre>{escaped}</pre>
</section>"""


def _timescale_to_ns(timescale: str) -> float:
    """Return multiplier: raw_vcd_time * result = nanoseconds."""
    m = re.match(r"^\s*(\d+)\s*(fs|ps|ns|us|ms|s)\s*$", timescale.strip(), re.I)
    if not m:
        return 1.0
    amount = int(m.group(1))
    unit = m.group(2).lower()
    factors = {
        "fs": 1e-6,
        "ps": 1e-3,
        "ns": 1.0,
        "us": 1e3,
        "ms": 1e6,
        "s": 1e9,
    }
    return amount * factors.get(unit, 1.0)


def load_module_info(work_dir: Path) -> Optional[Dict[str, Any]]:
    """Load dut_info.json written by the verification pipeline."""
    path = work_dir / "dut_info.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_dut_info(work_dir: Path, module: RtlModule) -> None:
    """Persist port metadata for waveform signal grouping."""
    info = {
        "module": module.name,
        "ports": [
            {
                "name": p.name,
                "direction": p.direction.value,
                "width": p.width,
            }
            for p in module.ports
        ],
    }
    (work_dir / "dut_info.json").write_text(
        json.dumps(info, indent=2),
        encoding="utf-8",
    )


def _signal_group(
    name: str,
    module: Optional[RtlModule] = None,
    module_info: Optional[Dict[str, Any]] = None,
) -> str:
    if module is not None:
        for p in module.ports:
            if p.name == name:
                if p.direction == PortDirection.INPUT:
                    return "inputs"
                if p.direction == PortDirection.OUTPUT:
                    return "outputs"
                if p.direction == PortDirection.INOUT:
                    return "inouts"
    if module_info and "ports" in module_info:
        for port in module_info["ports"]:
            if port.get("name") == name:
                d = str(port.get("direction", "")).lower()
                if d == "input":
                    return "inputs"
                if d == "output":
                    return "outputs"
                if d == "inout":
                    return "inouts"
    low = name.lower()
    if low in _TB_NAME_HINTS:
        return "testbench"
    if any(low.endswith(s) for s in _TB_SUFFIXES):
        return "testbench"
    if low in {"clk", "clock", "ck"}:
        return "testbench"
    return "unknown"


def _parse_vcd_events(vcd_path: Path) -> Tuple[str, float, Dict[str, Dict[str, Any]]]:
    """
    Parse VCD into per-signal transition lists keyed by signal name.
    Returns (timescale_str, ns_multiplier, signals_dict).
    """
    lines = vcd_path.read_text(encoding="utf-8", errors="replace").splitlines()
    timescale = "1ns"
    ns_mult = 1.0
    scope_stack: List[str] = []
    sym_to_name: Dict[str, str] = {}
    sym_width: Dict[str, int] = {}
    # name -> {width, transitions: [{time, value}]}
    by_name: Dict[str, Dict[str, Any]] = {}
    current_time = 0
    definitions_done = False
    pending_timescale = False

    def ensure_signal(sig_name: str, sym: str, width: int) -> None:
        if sig_name not in by_name:
            by_name[sig_name] = {"width": width, "sym": sym, "transitions": []}
        else:
            by_name[sig_name]["width"] = max(by_name[sig_name]["width"], width)

    def append_transition(sig_name: str, t: int, val: str) -> None:
        tr = by_name[sig_name]["transitions"]
        if tr and tr[-1]["time"] == t and tr[-1]["value"] == val:
            return
        if tr and tr[-1]["time"] == t:
            tr[-1]["value"] = val
        else:
            tr.append({"time": t, "value": val})

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("$timescale"):
            m = re.search(r"\$timescale\s+(.+?)\s+\$end", line)
            if m:
                timescale = m.group(1).strip()
                ns_mult = _timescale_to_ns(timescale)
            else:
                pending_timescale = True
            continue
        if pending_timescale:
            if line.startswith("$"):
                pending_timescale = False
            elif line.strip() and not line.startswith("$"):
                timescale = line.strip()
                ns_mult = _timescale_to_ns(timescale)
                pending_timescale = False
                continue
        if line.startswith("$scope"):
            parts = line.split()
            if len(parts) >= 3:
                scope_stack.append(parts[2])
            continue
        if line.startswith("$upscope"):
            if scope_stack:
                scope_stack.pop()
            continue
        if line.startswith("$enddefinitions"):
            definitions_done = True
            continue
        if line.startswith("$var"):
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                width = int(parts[2])
            except ValueError:
                width = 1
            sym = parts[3]
            name = parts[4]
            sym_to_name[sym] = name
            sym_width[sym] = width
            ensure_signal(name, sym, width)
            continue
        if not definitions_done:
            continue
        if line.startswith("#"):
            try:
                current_time = int(line[1:].split()[0])
            except ValueError:
                continue
            continue
        if line and line[0] == "b":
            m = re.match(r"b([01xzXZ?]+)\s*(\S+)", line)
            if m:
                val, sym = m.group(1), m.group(2)
                name = sym_to_name.get(sym, sym)
                w = sym_width.get(sym, by_name.get(name, {}).get("width", 1))
                ensure_signal(name, sym, w)
                append_transition(name, current_time, val.upper())
            continue
        if line and line[0] in "01xzXZ?":
            m = re.match(r"([01xzXZ?]+)\s*(\S+)", line)
            if m:
                val, sym = m.group(1), m.group(2)
                name = sym_to_name.get(sym, sym)
                w = sym_width.get(sym, 1)
                ensure_signal(name, sym, w)
                append_transition(name, current_time, val.upper())
            continue

    return timescale, ns_mult, by_name


def vcd_to_json(
    vcd_path: Path,
    module: Optional[RtlModule] = None,
    module_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Parse VCD into structured JSON for visual rendering."""
    if not vcd_path.is_file():
        return {"error": "No waveform available"}

    try:
        timescale, ns_mult, by_name = _parse_vcd_events(vcd_path)
    except OSError as e:
        return {"error": str(e)}

    if not by_name:
        return {"error": "VCD parsed but no signals found"}

    end_time = 0
    signals: List[Dict[str, Any]] = []
    for name in sorted(by_name.keys()):
        meta = by_name[name]
        transitions = meta["transitions"]
        if not transitions:
            transitions = [{"time": 0, "value": "0" if meta["width"] == 1 else "0" * meta["width"]}]
        for tr in transitions:
            end_time = max(end_time, tr["time"])
        width = meta["width"]
        pad = width
        norm: List[Dict[str, Any]] = []
        for tr in transitions:
            val = tr["value"]
            if val.lower() in ("x", "z"):
                norm.append({"time": tr["time"], "value": val[0].upper()})
            elif len(val) == 1 and width > 1:
                norm.append({"time": tr["time"], "value": val.zfill(pad)})
            else:
                norm.append({"time": tr["time"], "value": val.zfill(pad) if val.isdigit() else val})
        signals.append(
            {
                "name": name,
                "width": width,
                "group": _signal_group(name, module=module, module_info=module_info),
                "transitions": norm,
            }
        )

    return {
        "timescale": timescale,
        "timescale_to_ns": ns_mult,
        "end_time": end_time,
        "end_time_ns": end_time * ns_mult,
        "signals": signals,
    }
