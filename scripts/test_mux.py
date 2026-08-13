import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rtl_verify.always_model import extract_always_blocks, _eval_if_chain
from rtl_verify.combinational_model import _eval_verilog_expr

rtl = Path("examples/mux_4to1.v").read_text()
body = extract_always_blocks(rtl)[0]
env = {"d0": 1, "d1": 2, "d2": 3, "d3": 4, "sel": 0, "y": 0}
expr = "sel == 2'b00"
print("eval cond...", flush=True)
print(_eval_verilog_expr(expr, env), flush=True)
print("if chain...", flush=True)
print(_eval_if_chain(body, env), flush=True)
