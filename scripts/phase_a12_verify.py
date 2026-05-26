"""Phase A.12 — run all 9 designs + adversarial tests; print mandatory report."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl
from rtl_verify.backends.registry import get_backend
from rtl_verify.combinational_model import can_self_check
from rtl_verify.generators.base import TbLanguage
from rtl_verify.pipeline import run_verification
from rtl_verify.sim_results import parse_test_results
from rtl_verify.verification_mode import resolve_status

DESIGNS = [
    ("adder_2bit", "Test 1"),
    ("full_adder_4bit", "Test 2"),
    ("alu_4bit", "Test 3"),
    ("mux_4to1", "Test 4"),
    ("priority_encoder_4bit", "Test 5"),
    ("mux_ternary", "Test 6"),
    ("parity_gen", "Test 7"),
    ("signed_lt", "Test 8"),
    ("mixed_design", "Test 9"),
]


def run_design(name: str) -> dict:
    rtl_path = ROOT / "examples" / f"{name}.v"
    rtl = rtl_path.read_text(encoding="utf-8")
    mod = analyze_rtl(rtl)
    work = ROOT / "work_verify" / f"a12_{name}"
    result = run_verification(
        rtl,
        language=TbLanguage.SYSTEMVERILOG,
        top_module=mod.name,
        work_dir=work,
        backend="icarus",
    )
    tests = parse_test_results(result.sim_log or "", mod=mod, rtl_source=rtl)
    exp_ok = bool(tests) and all(t.get("expected") for t in tests)
    report = (work / "report.txt").read_text(encoding="utf-8") if (work / "report.txt").exists() else ""
    inf_m = re.search(r"Inferred operation: (.+)", report)
    inferred = inf_m.group(1).strip() if inf_m else mod.inferred_op
    sample_exp = ""
    if tests and tests[0].get("expected"):
        sample_exp = " ".join(f"{k}={v}" for k, v in tests[0]["expected"].items())
    cov = {}
    cp = work / "coverage.json"
    if cp.exists():
        cov = json.loads(cp.read_text(encoding="utf-8"))
    return {
        "name": name,
        "verdict": result.verdict,
        "inferred": inferred,
        "sample_exp": sample_exp,
        "tests": len(tests),
        "exp_ok": exp_ok,
        "sc": can_self_check(rtl, mod),
        "coverage": cov,
        "sim_log": result.sim_log or "",
    }


def corrupt_test(name: str, corrupt_fn, good_rtl: str) -> dict:
    mod = analyze_rtl(good_rtl)
    work = ROOT / "work_verify" / f"a12_corrupt_{name}"
    work.mkdir(parents=True, exist_ok=True)
    r0 = run_verification(
        good_rtl,
        language=TbLanguage.SYSTEMVERILOG,
        work_dir=work,
        backend="icarus",
    )
    corrupt = corrupt_fn(good_rtl)
    (work / "dut.v").write_text(corrupt, encoding="utf-8")
    backend = get_backend("icarus")
    sim = backend.run(work / "dut.v", work / "tb.v", work, top=f"tb_{mod.name}")
    mode, verdict, _, _ = resolve_status(mod, good_rtl, sim.log)
    fails = [ln for ln in sim.log.splitlines() if "RESULT,FAIL" in ln.upper() or 'RESULT","FAIL' in ln]
    if not fails:
        fails = [ln for ln in sim.log.splitlines() if ",FAIL" in ln and ln.strip().startswith("TEST,")]
    return {"verdict": verdict, "fail_count": len(fails), "sample": fails[0] if fails else ""}


def main() -> int:
    ok = True
    print("## Test results — all 9 designs\n")
    for name, label in DESIGNS:
        r = run_design(name)
        good = r["verdict"] == "pass" and r["exp_ok"] and r["sc"]
        ok = ok and good
        print(f"### {label}: {name}")
        print(f"Verdict: {r['verdict'].upper()}")
        print(f"Inferred operation: {r['inferred']}")
        print(f"Sample EXPECTED: {r['sample_exp'] or '(none)'}")
        print(f"Tests: {r['tests']} exp_ok={r['exp_ok']}")
        print()

    print("## Adversarial tests\n")
    alu_rtl = (ROOT / "examples" / "alu_4bit.v").read_text(encoding="utf-8")
    ca = corrupt_test(
        "alu",
        lambda s: s.replace("3'b000: result = a + b;", "3'b000: result = a - b;", 1),
        alu_rtl,
    )
    print("### Corrupted ALU")
    print(f"Result: {ca['verdict'].upper()}")
    print(f"Failing cases: {ca['fail_count']}")
    print(f"Sample: {ca['sample'][:200]}")
    ok = ok and ca["verdict"] == "fail"

    pe_rtl = (ROOT / "examples" / "priority_encoder_4bit.v").read_text(encoding="utf-8")
    cp = corrupt_test(
        "pe",
        lambda s: s.replace("grant = 2'd3", "grant = 2'd0", 1),
        pe_rtl,
    )
    print("\n### Corrupted priority encoder")
    print(f"Result: {cp['verdict'].upper()}")
    print(f"Failing cases: {cp['fail_count']}")
    print(f"Sample: {cp['sample'][:200]}")
    ok = ok and cp["verdict"] == "fail"

    alu_cov = run_design("alu_4bit")
    stmt = alu_cov["coverage"].get("statement", {})
    br = alu_cov["coverage"].get("branch", {})
    tg = alu_cov["coverage"].get("toggle", {})
    print("\n## Coverage on ALU")
    print(f"Statement: {stmt.get('percent', 0)}%")
    print(f"Branch: {br.get('percent', 0)}% ({br.get('hit', 0)} arms hit)")
    print(f"Toggle: {tg.get('percent', 0)}%")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
