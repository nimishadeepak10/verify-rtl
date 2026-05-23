"""VCD parsing: text trace summary and simple HTML waveform viewer."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple


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
