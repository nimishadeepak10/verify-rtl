"""Cut points + assertion decomposition (abstraction techniques adapted from
Seligman et al.'s "Formal Verification" Ch.10, Siemens Verification
Horizons' complexity-reduction series, and the FVM complexity guide).

Part 1: decomposition is an exact Boolean equivalence -- checked by brute
force truth table, not by eye.
Part 2: cut-point validation rejects bad names instead of silently no-oping.
Part 3: real solver runs. A genuinely stuck proof (600-stage MAC pipeline
feeding a wide accumulator that gates an arbiter) is rescued by a cut point,
manual and automatic; a cut point on a counter is shown to produce an
artifact FALSIFIED trace (the documented caveat), not a real bug.

Part 3 must be run via PowerShell, not Bash (see project memory on yosys
subprocess spawning).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.assertion_decompose import decompose_assertion  # noqa: E402
from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.cutpoint import recommend_cutpoint_candidates, validate_cutpoints  # noqa: E402


def _truth(expr: str, names, env) -> bool:
    py = expr.replace("||", " or ").replace("&&", " and ")
    py = re.sub(r"!(?!=)", " not ", py)
    return bool(eval(py, {}, dict(zip(names, env))))


def check_equivalent(original: str, parts: list[str], names) -> None:
    for env in itertools.product([False, True], repeat=len(names)):
        want = _truth(original, names, env)
        got = all(_truth(p, names, env) for p in parts)
        assert want == got, (original, parts, dict(zip(names, env)))


def part1_decomposition() -> None:
    print("=== Part 1: decomposition is an exact equivalence ===")
    cases = [
        ("!(a || b) || c", 2, ["!(a) || (c)", "!(b) || (c)"]),
        ("!(d) || (e && f)", 2, ["!(d) || (e)", "!(d) || (f)"]),
        ("!(a || b) || (c && d)", 4, None),
        ("a && b && c", 3, ["a", "b", "c"]),
        ("!(a) || (b || c)", 1, None),      # conclusion is an OR: must stay whole
        ("!(a && b) || c", 1, None),        # guard is an AND: must stay whole
        ("a | b", 1, None),                 # single-char operator: never split
        ("!(f(a || b)) || c", 1, None),     # OR nested in parens: not top level
    ]
    for expr, n_parts, expected in cases:
        parts = decompose_assertion(expr)
        assert len(parts) == n_parts, (expr, parts)
        if expected is not None:
            assert parts == expected, (expr, parts)
        if n_parts > 1:
            check_equivalent(expr, parts, ["a", "b", "c", "d", "e", "f"])
        print(f"  {expr!r:34} -> {len(parts)} part(s)")
    print("OK\n")


def part2_validation() -> None:
    print("=== Part 2: cut-point validation ===")
    src = (ROOT / "examples" / "free_running_counter.v").read_text(encoding="utf-8")
    mod = analyze_rtl(src, top_module="free_running_counter")
    ok, errs = validate_cutpoints(src, mod, ["count", "nope", "clk", "rst_n"])
    assert ok == ["count"], ok
    assert len(errs) == 3, errs
    print(f"  usable={ok}  rejected={len(errs)}")
    cands = recommend_cutpoint_candidates(mod, src)
    assert [c.signal for c in cands] == ["count"] and cands[0].reason == "wide_counter", cands
    print(f"  candidate: {cands[0].signal} ({cands[0].reason}, {cands[0].width} bits)")
    print("OK\n")


def _wrapper_with_accumulator() -> str:
    arbiter = (ROOT / "examples" / "arbiter4.v").read_text(encoding="utf-8")
    mac = (ROOT / "examples" / "wide_mac_pipeline.v").read_text(encoding="utf-8")
    wrapper = (ROOT / "examples" / "big_soc_wrapper.v").read_text(encoding="utf-8")
    wrapper = wrapper.replace("STAGES(200)", "STAGES(600)")
    old = "wire mac_busy = |mac_result[63:48];"
    assert old in wrapper
    wrapper = wrapper.replace(old, (
        "reg [31:0] mac_hash;\n"
        "    always @(posedge clk or negedge rst_n)\n"
        "        if (!rst_n) mac_hash <= 32'd0; else mac_hash <= mac_hash + mac_result[31:0];\n"
        "    wire mac_busy = mac_hash[31];"
    ))
    return "\n".join([arbiter, mac, wrapper])


PROP = [{"name": "grant_onehot0", "expr": "(grant == 4'd0) || $onehot(grant)", "kind": "assert"}]


def part3_real_solver() -> None:
    from _formal_call import formal_check

    source = _wrapper_with_accumulator()

    def run(**kw):
        args = dict(rtl_file=None, rtl_text=source, top_module="big_soc_wrapper",
                    properties=json.dumps(PROP), timeout_sec=40, depth_override=0,
                    cross_check=True, blackbox_modules="", auto_blackbox=False,
                    cut_signals="", auto_cutpoint=False, decompose=False)
        args.update(kw)
        return asyncio.run(formal_check(**args))

    print("=== Part 3a: plain run is genuinely stuck ===")
    plain = run()["properties"][0]
    print(f"  verdict={plain['verdict']} attempts={[(a['label'], a['status']) for a in plain['attempts']]}")
    assert plain["verdict"] in ("TIMEOUT", "UNKNOWN"), plain["verdict"]
    print("OK\n")

    print("=== Part 3b: manual cut_signals=mac_hash rescues it; cross-check skipped ===")
    manual = run(cut_signals="mac_hash")["properties"][0]
    print(f"  verdict={manual['verdict']} cut_signals={manual['cut_signals']}")
    assert manual["verdict"] == "PROVEN", manual["verdict"]
    assert manual["cross_check"]["performed"] is False, manual["cross_check"]
    print("OK\n")

    print("=== Part 3c: auto_cutpoint finds the wide accumulator by itself ===")
    auto = run(auto_cutpoint=True)["properties"][0]
    info = auto["auto_cutpoint"]
    print(f"  verdict={auto['verdict']} auto_cutpoint={json.dumps(info)[:260]}")
    assert auto["verdict"] == "PROVEN", auto["verdict"]
    assert info["resolved"] and info["cut_signal"] == "mac_hash", info
    assert auto["cross_check"]["performed"] is False
    print("OK\n")

    print("=== Part 3d: bad cut name is an error, not a silent no-op ===")
    bad = run(cut_signals="not_a_signal")
    assert "error" in bad and "not_a_signal" in bad["error"], bad
    print(f"  {bad['error']}")
    print("OK\n")

    print("=== Part 3e: cutting a counter gives an ARTIFACT falsification (documented caveat) ===")
    counter = (ROOT / "examples" / "free_running_counter.v").read_text(encoding="utf-8")
    prop = [{"name": "never_200", "expr": "count != 8'd200", "kind": "assert"}]
    args = dict(rtl_file=None, rtl_text=counter, top_module="free_running_counter",
                properties=json.dumps(prop), timeout_sec=60, depth_override=0,
                cross_check=False, blackbox_modules="", auto_blackbox=False,
                cut_signals="", auto_cutpoint=False, decompose=False)
    real = asyncio.run(formal_check(**args))["properties"][0]
    cut = asyncio.run(formal_check(**{**args, "cut_signals": "count"}))["properties"][0]
    print(f"  real: {real['verdict']}   with count cut: {cut['verdict']}")
    assert real["verdict"] == "FALSIFIED" and cut["verdict"] == "FALSIFIED"
    print("OK (both FALSIFIED -- the cut trace is the artifact the caveat warns about)\n")

    print("=== Part 3f: decompose=True splits and aggregates ===")
    arb = (ROOT / "examples" / "arbiter4.v").read_text(encoding="utf-8")
    dprops = [{"name": "grant_ok",
               "expr": "!(grant != 4'd0) || ($onehot(grant) && (grant & ~req) == 4'd0)",
               "kind": "assert"}]
    out = asyncio.run(formal_check(
        rtl_file=None, rtl_text=arb, top_module="arbiter4", properties=json.dumps(dprops),
        timeout_sec=60, depth_override=0, cross_check=False, blackbox_modules="",
        auto_blackbox=False, cut_signals="", auto_cutpoint=False, decompose=True))
    names = [r["name"] for r in out["properties"]]
    print(f"  parts run: {names}")
    print(f"  decomposition: {json.dumps(out['decomposition'])[:300]}")
    assert names == ["grant_ok__part1", "grant_ok__part2"], names
    assert out["decomposition"][0]["original"] == "grant_ok"
    print("OK\n")


if __name__ == "__main__":
    part1_decomposition()
    part2_validation()
    if "--solver" in sys.argv:
        part3_real_solver()
    print("All requested parts passed.")
