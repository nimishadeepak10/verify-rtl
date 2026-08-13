"""End-to-end verification check for contractual designs."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl
from rtl_verify.combinational_model import can_self_check, expected_outputs
from rtl_verify.generators.base import TbLanguage
from rtl_verify.pipeline import run_verification
from rtl_verify.sim_results import parse_test_results

DESIGNS = ["adder_2bit", "full_adder_4bit", "alu_4bit", "mux_4to1"]


def check_design(name: str) -> dict:
    rtl_path = ROOT / "examples" / f"{name}.v"
    rtl = rtl_path.read_text(encoding="utf-8")
    mod = analyze_rtl(rtl)
    sc = can_self_check(rtl, mod)
    result = run_verification(
        rtl,
        language=TbLanguage.SYSTEMVERILOG,
        top_module=mod.name,
        work_dir=ROOT / "work_verify" / name,
        backend="icarus",
    )
    tests = parse_test_results(result.sim_log or "", mod=mod, rtl_source=rtl)
    exp_ok = all(t.get("expected") for t in tests) if tests else False
    pass_ok = result.verdict == "pass" and all(t.get("result") == "PASS" for t in tests)
    return {
        "name": name,
        "self_check": sc,
        "inferred": mod.inferred_op,
        "verdict": result.verdict,
        "mode": result.verification_mode,
        "tests": len(tests),
        "exp_ok": exp_ok,
        "pass_ok": pass_ok,
        "coverage": (result.work_dir / "coverage.json").exists(),
    }


def main() -> int:
    ok = True
    for name in DESIGNS:
        r = check_design(name)
        good = r["self_check"] and r["pass_ok"] and r["exp_ok"] and r["verdict"] == "pass"
        ok = ok and good
        print(
            f"{name}: verdict={r['verdict']} mode={r['mode']} "
            f"tests={r['tests']} exp_ok={r['exp_ok']} inferred={r['inferred']!r}"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
