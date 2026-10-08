"""Parameter reduction + ROM-to-case (abstraction techniques adapted from
Seligman et al.'s "Formal Verification" Ch.6/Ch.10, Siemens Verification
Horizons' complexity-reduction series, and the FVM complexity guide).

Parts 1-2 are static (no solver). Part 3 (--solver) needs real engines and
must be run via PowerShell, not Bash (see project memory on yosys spawning).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from rtl_verify.param_reduce import (  # noqa: E402
    apply_parameter_overrides, list_parameters, parse_override_spec, recommend_parameter_reductions,
)
from rtl_verify.rom_abstract import convert_roms_to_case, find_rom_candidates  # noqa: E402

FIFO = """
module pfifo #(
    parameter integer DEPTH = 1024,
    parameter integer WIDTH = 64,
    parameter MODE = 3
) (input clk, input [WIDTH-1:0] d, output [WIDTH-1:0] q);
    localparam AW = 10;
    pfifo_child #(.NUM_ENTRIES(256), .TAG(7)) u (.clk(clk));
endmodule
"""

ROM_OK = """
module rom_lookup(input clk, input [3:0] addr, output reg [7:0] data);
    reg [7:0] rom [0:15];
    initial begin
        rom[0] = 8'h00; rom[1] = 8'h11; rom[2] = 8'h22; rom[3] = 8'h33;
        rom[4] = 8'h44; rom[5] = 8'h55; rom[6] = 8'h66; rom[7] = 8'h77;
        rom[8] = 8'h88; rom[9] = 8'h99; rom[10] = 8'hAA; rom[11] = 8'hBB;
        rom[12] = 8'hCC; rom[13] = 8'hDD; rom[14] = 8'hEE; rom[15] = 8'hFF;
    end
    always @(posedge clk) data <= rom[addr];
endmodule
"""

RAM = """
module ram1(input clk, input we, input [1:0] a, input [7:0] d, output reg [7:0] q);
    reg [7:0] m [0:3];
    initial begin m[0] = 8'h1; end
    always @(posedge clk) begin if (we) m[a] <= d; q <= m[a]; end
endmodule
"""

READMEM = """
module rm(input clk, input [1:0] a, output reg [7:0] q);
    reg [7:0] m [0:3];
    initial $readmemh("x.hex", m);
    always @(posedge clk) q <= m[a];
endmodule
"""

BITSEL = """
module bs(input clk, input [1:0] a, output reg q);
    reg [7:0] m [0:3];
    initial begin m[0] = 8'h1; m[1] = 8'h2; end
    always @(posedge clk) q <= m[a][3];
endmodule
"""


def part1_params() -> None:
    print("=== Part 1: parameter discovery / reduction / rewriting ===")
    found = list_parameters(FIFO, "pfifo")
    print(f"  found: {found}")
    names = {(n, w) for n, _v, w in found}
    assert ("DEPTH", "module_default") in names and ("WIDTH", "module_default") in names
    assert ("NUM_ENTRIES", "instance_override") in names and ("TAG", "instance_override") in names
    assert not any(n == "AW" for n, _v, _w in found), "localparam must never be touched"

    recs = recommend_parameter_reductions(FIFO, "pfifo")
    got = {r.name: r.proposed for r in recs}
    print(f"  auto-proposed: {got}")
    assert got == {"DEPTH": 4, "NUM_ENTRIES": 4}, got   # WIDTH (width) and MODE/TAG (other/small) never auto

    new, applied, missing = apply_parameter_overrides(FIFO, "pfifo", {"DEPTH": 4, "WIDTH": 8, "NUM_ENTRIES": 4, "GHOST": 1})
    assert "DEPTH = 4" in new and "WIDTH = 8" in new and ".NUM_ENTRIES(4)" in new, new
    assert "localparam AW = 10" in new and "parameter MODE = 3" in new
    assert missing == ["GHOST"], missing
    print(f"  applied={len(applied)} missing={missing}")

    ov, errs = parse_override_spec("DEPTH=4, X=oops, Y=2")
    assert ov == {"DEPTH": 4, "Y": 2} and len(errs) == 1
    print("OK\n")


def part2_rom() -> None:
    print("=== Part 2: ROM eligibility + rewrite ===")
    [ok] = find_rom_candidates(ROM_OK)
    assert ok.eligible and ok.depth == 16 and ok.width == 8 and len(ok.entries) == 16, ok
    [ram] = find_rom_candidates(RAM)
    assert not ram.eligible and "RAM" in ram.reason, ram
    [rm] = find_rom_candidates(READMEM)
    assert not rm.eligible and "readmem" in rm.reason, rm
    [bs] = find_rom_candidates(BITSEL)
    assert not bs.eligible and "bit-selected" in bs.reason, bs
    print(f"  ROM ok: {ok.depth}x{ok.width} eligible; RAM / $readmemh / bit-select refused with reasons")

    new, _ = convert_roms_to_case(ROM_OK)
    assert "reg [7:0] rom" not in new and "rom__rom(addr)" in new and "initial" in new
    assert "rom[0]" not in new and "default: rom__rom" in new, new
    assert find_rom_candidates(ROM_OK)[0].state_bits_saved == 128
    untouched, _ = convert_roms_to_case(RAM)
    assert untouched == RAM, "an ineligible memory must be left byte-for-byte alone"
    print("  rewrite removes the array, the initial writes, and routes reads through the lookup; "
          "RAM left byte-for-byte untouched")
    print("OK\n")


def part3_solver() -> None:
    from _formal_call import formal_check
    import test_cutpoint_and_decompose as t

    def call(source, top, props, **kw):
        args = dict(rtl_file=None, rtl_text=source, top_module=top, properties=json.dumps(props),
                    timeout_sec=40, depth_override=0, cross_check=True, blackbox_modules="",
                    auto_blackbox=False, cut_signals="", auto_cutpoint=False, decompose=False,
                    rom_to_case=False, param_overrides="", auto_param_reduction=False)
        args.update(kw)
        return asyncio.run(formal_check(**args))

    print("=== Part 3a: ROM-to-case preserves verdicts (PROVEN and FALSIFIED) ===")
    good = [{"name": "rom_ok", "expr": "data == {$past(addr), $past(addr)}", "kind": "assert"}]
    bad = [{"name": "rom_wrong", "expr": "data == {$past(addr), 4'd0}", "kind": "assert"}]
    for label, props, want in (("good", good, "PROVEN"), ("bad", bad, "FALSIFIED")):
        plain = call(ROM_OK, "rom_lookup", props)["properties"][0]["verdict"]
        conv_out = call(ROM_OK, "rom_lookup", props, rom_to_case=True)
        conv = conv_out["properties"][0]["verdict"]
        print(f"  {label}: original={plain}  rom_to_case={conv}  report={conv_out['rom_to_case'][0]['state_bits_removed']} bits removed")
        assert conv == want, (label, conv)
    print("OK\n")

    print("=== Part 3b: reduced-config proof is NOT a full proof (documented caveat) ===")
    counter = """
module sized_counter #(parameter LIMIT = 16) (
    input clk, input rst_n, input inc, output reg [4:0] c);
    always @(posedge clk or negedge rst_n)
        if (!rst_n) c <= 5'd0;
        else if (inc && c < LIMIT) c <= c + 5'd1;
endmodule
"""
    prop = [{"name": "never_10", "expr": "c != 5'd10", "kind": "assert"}]
    full = call(counter, "sized_counter", prop, cross_check=False)["properties"][0]["verdict"]
    red_out = call(counter, "sized_counter", prop, cross_check=False, param_overrides="LIMIT=4")
    red = red_out["properties"][0]["verdict"]
    print(f"  full config: {full}   LIMIT=4: {red}")
    assert full == "FALSIFIED" and red == "PROVEN", (full, red)
    assert "BOUNDED CONFIGURATION" in red_out["parameter_reduction"]["caveat"]
    print("OK (the reduced PROVEN is wrong for the real design; the response says so)\n")

    print("=== Part 3c: stuck 300-stage proof -> manual + auto parameter reduction ===")
    source = t._wrapper_with_accumulator()  # 300-stage pipeline; PDR cannot solve it plain
    props = t.PROP
    plain = call(source, "big_soc_wrapper", props)["properties"][0]
    print(f"  plain: {plain['verdict']}")
    assert plain["attempts"][0]["status"] != "PASS", plain["attempts"]   # PDR stuck; a later k-induction may still win
    man = call(source, "big_soc_wrapper", props, param_overrides="STAGES=4")["properties"][0]
    print(f"  STAGES=4: {man['verdict']}  cross_check_performed={man['cross_check']['performed']}")
    assert man["verdict"] == "PROVEN" and man["cross_check"]["performed"] is False
    auto = call(source, "big_soc_wrapper", props, auto_param_reduction=True)["properties"][0]
    info = auto.get("auto_param_reduction") or {}
    print(f"  auto: {auto['verdict']}  {json.dumps(info)[:230]}")
    assert auto["verdict"] == "PROVEN", auto["verdict"]
    if info.get("resolved"):      # unless a slow k-induction solved the full design first
        assert info["overrides"] == {"STAGES": 4}, info
        assert info["bounded_configuration"] is True
    print("OK\n")

    print("=== Part 3d: bad override names are errors, not silent no-ops ===")
    err = call(counter, "sized_counter", prop, param_overrides="NOPE=3")
    assert "error" in err and "NOPE" in err["error"], err
    print(f"  {err['error']}")
    print("OK\n")


if __name__ == "__main__":
    part1_params()
    part2_rom()
    if "--solver" in sys.argv:
        part3_solver()
    print("All requested parts passed.")
