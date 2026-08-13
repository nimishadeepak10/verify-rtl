import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rtl_verify.combinational_model import _eval_verilog_expr, _apply_signed_wrappers

from rtl_verify.analyzer import analyze_rtl
from rtl_verify.combinational_model import _port_values

rtl = Path("examples/signed_lt.v").read_text()
mod = analyze_rtl(rtl)
env = _port_values(mod, {"a": 0xFF, "b": 0x01})
widths = {p.name: p.width for p in mod.ports}
print("env", env, "widths", widths)
expr = "($signed(a) < $signed(b))"
e2 = _apply_signed_wrappers(expr, env, widths)
print("after signed:", e2)
print("result:", _eval_verilog_expr(expr, env, widths))
from rtl_verify.combinational_model import (
    _apply_signed_wrappers,
    _apply_reductions,
    _normalize_verilog_numbers,
)
e = expr
e = _apply_signed_wrappers(e, env, widths)
print("signed wrap:", e)
e = _apply_reductions(e, env, widths)
e = _normalize_verilog_numbers(e)
for name, val in sorted(env.items(), key=lambda x: -len(x[0])):
    import re

    e = re.sub(rf"\b{re.escape(name)}\b", str(val), e)
print("after sub:", e)
