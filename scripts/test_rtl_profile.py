"""Demo: parse RTL profile for priority_weighted_arbiter."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.rtl_profile import parse_rtl_profile  # noqa: E402

ARBITER = ROOT / "examples" / "priority_weighted_arbiter.sv"
if not ARBITER.is_file():
    ARBITER = ROOT / "work_verify" / "arbiter_vivado" / "dut.v"


def main() -> None:
    rtl = ARBITER.read_text(encoding="utf-8")
    profile = parse_rtl_profile(rtl)
    d = profile.to_dict()
    print(json.dumps(d, indent=2))
    print()
    print("--- summary ---")
    print(f"module: {profile.module_name}")
    print(f"kind: {profile.design_kind}")
    print(f"parameters: {[p.name for p in profile.parameters]}")
    print("ports:")
    for p in profile.ports:
        kind = f" [{p.input_kind}]" if p.input_kind else ""
        packed = " packed" if p.is_packed_array else ""
        print(f"  {p.direction:6} {p.name:16} w={p.width}{packed}{kind}")
    print(f"internal comb signals: {profile.internal_comb_signals}")
    print(f"always_comb blocks: {len(profile.combinational_blocks)}")


if __name__ == "__main__":
    main()
