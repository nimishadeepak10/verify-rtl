"""End-to-end: RTL -> LLM suggests classified properties -> LLM converts each
to SVA -> each candidate actually runs through the real formal backend.

Real LLM calls, real SymbiYosys runs — not mocked. This is the actual
Phase 2 core engine, tested the same way as every other piece this
session: by running it and looking at what really comes back, not by
trusting what it should do.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.backends.symbiyosys import SymbiYosysBackend  # noqa: E402
from rtl_verify.formal_props import generate_formal_wrapper, recommended_formal_config  # noqa: E402
from rtl_verify.property_suggester import suggest_properties  # noqa: E402
from rtl_verify.property_to_sva import convert_to_sva  # noqa: E402


def main() -> None:
    rtl_path = ROOT / "examples" / "traffic_light_fsm.v"
    rtl_source = rtl_path.read_text(encoding="utf-8")
    module = analyze_rtl(rtl_source, top_module="traffic_light_fsm")

    print("=== Suggesting properties (LLM call 1) ===")
    proposals = suggest_properties(module, rtl_source, spec_text="")
    print(f"Got {len(proposals)} proposals:\n")
    for p in proposals:
        print(f"[{p['kind']:6s}] ({p['pattern']}) {p['description']}")
        print(f"         signals={p['signals']} rationale={p['rationale'][:100]}")
    assert 1 <= len(proposals) <= 15, f"unexpected proposal count: {len(proposals)}"
    assert all(p["kind"] in ("assert", "assume", "cover") for p in proposals)

    print("\n=== Converting each to SVA (LLM call 2) and running through SymbiYosys ===\n")
    for p in proposals:
        conv = convert_to_sva(module, p["kind"], p["description"], p.get("rationale", ""))
        if not conv["expressible"]:
            print(f"[{p['kind']}] {p['description']!r} -> NOT EXPRESSIBLE: {conv['note']}\n")
            continue
        expr = conv["expr"]
        print(f"[{p['kind']}] {p['description']!r} -> {expr!r}")

        wrapper_sv = generate_formal_wrapper(module, [(p["name"], expr, p["kind"])])
        config = recommended_formal_config(module, kind=p["kind"])

        work = Path(tempfile.mkdtemp(prefix=f"suggest_e2e_{p['name']}_"))
        wrapper_path = work / "wrapper.sv"
        wrapper_path.write_text(wrapper_sv, encoding="utf-8")

        backend = SymbiYosysBackend()
        result = backend.run(
            rtl_path, wrapper_path, work,
            top="traffic_light_fsm_formal_top",
            depth=config["depth"], mode=config["mode"], engine=config["engine"],
        )
        verdict = "PASS/REACHED" if result.success else "FAIL/UNREACHED"
        print(f"    -> ran under {config} -> {verdict} (trace={result.vcd_path})\n")


if __name__ == "__main__":
    main()
