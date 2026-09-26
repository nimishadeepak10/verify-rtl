"""Dwyer/Avrunin/Corbett specification-pattern taxonomy: the scope
dimension (Global/Before/After/Between/After-Until), previously missing
from this project even though the pattern dimension (Absence/Existence/
.../Response) was already adopted -- see spec_patterns.py's own module
docstring for exactly what was already in place versus what this adds.

Includes a real, LLM-based end-to-end check (property_suggester.py's
actual suggest_properties() call, not a mock) confirming the new
`scope`/`scope_detail` schema fields come back populated for real,
skipped gracefully if no Anthropic API key is configured.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.spec_patterns import (  # noqa: E402
    PATTERNS,
    SCOPES,
    summarize_pattern_scope_coverage,
)


def main() -> None:
    print("=== All properties Global-scoped, missing several patterns -> "
          "both gaps flagged, notes explain why it matters ===")
    props = [
        {"name": "p1", "kind": "assert", "pattern": "Universality", "scope": "Global"},
        {"name": "p2", "kind": "cover", "pattern": "Existence", "scope": "Global"},
    ]
    result = summarize_pattern_scope_coverage(props)
    print(f"  missing_patterns: {result['missing_patterns']}")
    print(f"  missing_scopes: {result['missing_scopes']}")
    assert result["pattern_counts"]["Universality"] == 1
    assert result["pattern_counts"]["Existence"] == 1
    assert "Absence" in result["missing_patterns"]
    assert "Precedence" in result["missing_patterns"]
    assert result["scope_counts"]["Global"] == 2
    assert set(result["missing_scopes"]) == {"Before", "After", "Between", "After-Until"}
    assert len(result["notes"]) == 2, result["notes"]
    print("OK\n")

    print("=== A full, varied property set -> nothing meaningful flagged as missing ===")
    varied = [
        {"name": p.lower().replace(" ", "_"), "kind": "assert", "pattern": p, "scope": s}
        for p, s in zip(PATTERNS, SCOPES * 2)
    ]
    result2 = summarize_pattern_scope_coverage(varied)
    print(f"  missing_patterns: {result2['missing_patterns']}")
    print(f"  missing_scopes: {result2['missing_scopes']}")
    assert result2["missing_patterns"] == [], result2["missing_patterns"]
    # Only After-Until is allowed to be unrepresented without a note --
    # confirmed by construction here (8 patterns cycled over 5 scopes
    # means every scope gets at least one property).
    print("OK\n")

    print("=== Unclassified (hand-written, non-LLM) properties are counted "
          "honestly, not guessed at or dropped ===")
    result3 = summarize_pattern_scope_coverage([{"name": "hand_written", "kind": "assert"}])
    assert result3["unclassified"] == 1, result3
    print("OK\n")

    print("=== Empty property list -> everything missing, no crash ===")
    result4 = summarize_pattern_scope_coverage([])
    assert result4["unclassified"] == 0
    assert len(result4["missing_patterns"]) == len(PATTERNS)
    print("OK\n")

    from rtl_verify.llm_client import LLMNotConfigured
    from rtl_verify.analyzer import analyze_rtl
    from rtl_verify.property_suggester import suggest_properties

    try:
        print("=== Real: an LLM-based suggestion call returns the new "
              "scope/scope_detail fields populated ===")
        source = (ROOT / "examples" / "sync_fifo.v").read_text(encoding="utf-8")
        mod = analyze_rtl(source, top_module="sync_fifo")
        proposals = suggest_properties(mod, source, spec_text="")
        assert proposals, "expected at least one proposed property"
        for p in proposals:
            assert p.get("pattern") in PATTERNS, p
            assert p.get("scope") in SCOPES, p
            assert "scope_detail" in p, p
        coverage = summarize_pattern_scope_coverage(proposals)
        print(f"  {len(proposals)} properties proposed")
        print(f"  pattern_counts: {coverage['pattern_counts']}")
        print(f"  scope_counts: {coverage['scope_counts']}")
        assert coverage["unclassified"] == 0, (
            "every LLM-proposed property must carry a recognized pattern/scope tag", coverage
        )
        print("OK\n")
    except LLMNotConfigured:
        print("(skipping real LLM suggestion check -- no Anthropic API key configured)\n")

    print("=== ALL CASES MATCHED EXPECTATIONS ===")


if __name__ == "__main__":
    main()
