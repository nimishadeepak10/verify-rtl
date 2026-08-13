"""Corrupt DUT only (TB keeps good golden literals) — must FAIL."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl
from rtl_verify.backends.registry import get_backend
from rtl_verify.generators.base import TbLanguage
from rtl_verify.pipeline import run_verification
from rtl_verify.verification_mode import resolve_status

good = (ROOT / "examples" / "alu_4bit.v").read_text(encoding="utf-8")
work = ROOT / "work_verify" / "alu_corrupt_test"
r1 = run_verification(
    good,
    language=TbLanguage.SYSTEMVERILOG,
    work_dir=work,
    backend="icarus",
)
assert r1.verdict == "pass", f"baseline should pass, got {r1.verdict}"

corrupt = good.replace("3'b000: result = a + b;", "3'b000: result = a - b;", 1)
(work / "dut.v").write_text(corrupt, encoding="utf-8")
backend = get_backend("icarus")
assert backend and backend.is_available()
sim = backend.run(work / "dut.v", work / "tb.v", work, top=f"tb_{r1.module.name}")
mode, verdict, _, _ = resolve_status(r1.module, good, sim.log)
print("after corrupt dut only:", verdict, "RESULT" in sim.log)
if verdict != "fail":
    raise SystemExit(1)
print("corruption test OK")
