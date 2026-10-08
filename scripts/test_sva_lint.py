"""Tests for sva_lint.py against the 4 real bug patterns it targets
(Seligman/Schubert/Achutha Kiran Kumar, "Formal Verification", Chapter 9),
plus a clean-code false-positive check against real committed RTL.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rtl_verify.sva_lint import lint_sva  # noqa: E402


def _rules(report):
    return {f.rule for f in report.findings}


def test_missing_semicolon():
    src = """
    module m(input clk, input a, input b);
        always @(posedge clk) begin
            check1: assert (a)
            check2: assert (b);
        end
    endmodule
    """
    report = lint_sva(src)
    assert "missing_semicolon" in _rules(report), report.findings
    print("PASS: missing_semicolon caught")


def test_missing_semicolon_not_flagged_with_else():
    src = """
    module m(input clk, input a);
        always @(posedge clk) begin
            check1: assert (a) else $error("bad");
        end
    endmodule
    """
    report = lint_sva(src)
    assert "missing_semicolon" not in _rules(report), report.findings
    print("PASS: else-terminated assertion not flagged")


def test_short_circuit_function():
    src = """
    module m(input clk, input status, input [3:0] state, input valid);
        function bit legal_state(input [3:0] current, input v);
            bit_ok: assert (!v || (current != '0));
            legal_state = v;
        endfunction
        always_comb begin
            if (status || legal_state(state, valid)) begin end
        end
    endmodule
    """
    report = lint_sva(src)
    assert "short_circuit_assertion_function" in _rules(report), report.findings
    print("PASS: short_circuit_assertion_function caught")


def test_short_circuit_function_first_operand_not_flagged():
    src = """
    module m(input clk, input status, input [3:0] state, input valid);
        function bit legal_state(input [3:0] current, input v);
            bit_ok: assert (!v || (current != '0));
            legal_state = v;
        endfunction
        always_comb begin
            if (legal_state(state, valid) || status) begin end
        end
    endmodule
    """
    report = lint_sva(src)
    assert "short_circuit_assertion_function" not in _rules(report), report.findings
    print("PASS: first-operand call not flagged")


def test_moving_sampled_index():
    src = """
    module m(input clk, input [31:0] index, input [15:0] req);
        always @(posedge clk) begin
            current_pos_stable: assert ($past(req[index]));
        end
    endmodule
    """
    report = lint_sva(src)
    assert "moving_sampled_index" in _rules(report), report.findings
    print("PASS: moving_sampled_index caught")


def test_genvar_index_not_flagged():
    src = """
    module m(input clk);
        wire [1:0] state [0:2];
        genvar gp;
        generate
            for (gp = 0; gp < 3; gp = gp + 1) begin : G
                always @(posedge clk) begin
                    assert (!$past(state[gp][0]) || state[gp] != 2'd0);
                end
            end
        endgenerate
    endmodule
    """
    report = lint_sva(src)
    assert "moving_sampled_index" not in _rules(report), report.findings
    print("PASS: genvar-indexed $past not flagged")


def test_generate_label_collision():
    src = """
    module m(input clk);
        wire [1:0] state [0:2];
        genvar gp;
        generate
            for (gp = 0; gp < 3; gp = gp + 1) begin : G
                always @(posedge clk) begin
                    valid_encoding: assert (state[gp] <= 2'd3);
                end
            end
        endgenerate
    endmodule
    """
    report = lint_sva(src)
    assert "generate_label_collision" in _rules(report), report.findings
    print("PASS: generate_label_collision caught")


def test_real_mesi_rtl_is_clean():
    base = Path(r"C:\Users\Nimisha\mesi-cache-coherence-verification\rtl")
    for name in ["mesi_multi_cache.v", "mesi_line.v", "mesi_cache_core.v"]:
        src = (base / name).read_text(encoding="utf-8")
        report = lint_sva(src)
        assert report.findings == [], f"{name}: unexpected findings: {report.findings}"
    print("PASS: real committed MESI RTL has zero lint findings (no false positives)")


if __name__ == "__main__":
    test_missing_semicolon()
    test_missing_semicolon_not_flagged_with_else()
    test_short_circuit_function()
    test_short_circuit_function_first_operand_not_flagged()
    test_moving_sampled_index()
    test_genvar_index_not_flagged()
    test_generate_label_collision()
    test_real_mesi_rtl_is_clean()
    print("\nAll sva_lint tests passed.")
