"""Generate and optionally simulate self-checking combinational TB."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.generators.comb_assert_tb import generate  # noqa: E402
from rtl_verify.rtl_profile import parse_rtl_profile  # noqa: E402


def main() -> None:
    rtl_path = ROOT / "examples" / "priority_weighted_arbiter.sv"
    rtl = rtl_path.read_text(encoding="utf-8")
    mod = analyze_rtl(rtl)
    profile = parse_rtl_profile(rtl)

    tb_from_mod = generate(mod)
    tb_from_dict = generate(profile.to_dict())

    out = ROOT / "work_verify" / "arbiter_assert_tb.sv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tb_from_mod, encoding="utf-8")
    print(f"Wrote {out} ({len(tb_from_mod.splitlines())} lines)")
    assert "tb_check_outputs" in tb_from_mod
    assert "RESULT: PASS" in tb_from_mod
    assert len(tb_from_dict) > 1000

    try:
        from rtl_verify.backends.registry import get_backend

        vivado = get_backend("vivado")
        if vivado and vivado.is_available():
            work = Path(tempfile.mkdtemp(prefix="assert_tb_"))
            dut = work / "dut.sv"
            tb = work / "tb.sv"
            dut.write_text(rtl, encoding="utf-8")
            tb.write_text(tb_from_mod, encoding="utf-8")
            result = vivado.run(dut, tb, work, top=f"tb_{mod.name}")
            print(result.log[-2000:] if len(result.log) > 2000 else result.log)
            print("PASS" if "RESULT: PASS" in result.log else "SIM DID NOT PASS")
        else:
            print("Vivado not available — skipped simulation")
    except Exception as exc:
        print(f"Simulation skipped: {exc}")


if __name__ == "__main__":
    main()
