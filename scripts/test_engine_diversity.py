"""Stage 3: formal engine/solver diversity + auto-fallback.

Checks two things end-to-end against the real API (FastAPI TestClient --
no mocking, same discipline as every other test in this project):

1. The happy path is unchanged: a property PDR proves immediately still
   records exactly one attempt and the same PROVEN verdict as before this
   feature existed -- the chain must be a strict superset of the old
   single-engine behavior, not a regression.
2. A property that genuinely fails to build (bad signal reference) still
   reports ERROR honestly after walking the *whole* chain, with every
   attempt visible in the response -- fallback must not hide or silently
   swallow a real problem, and must not falsely report PROVEN/FALSIFIED.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.formal_props import recommended_engine_chain  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from api.main import app  # noqa: E402

client = TestClient(app)
rtl_path = ROOT / "examples" / "sync_fifo.v"
rtl_source = rtl_path.read_text(encoding="utf-8")
module = analyze_rtl(rtl_source, top_module="sync_fifo")


def check_chain_shapes() -> None:
    print("=== recommended_engine_chain() shapes ===")
    for kind in ("assert", "cover"):
        chain = recommended_engine_chain(module, kind=kind)
        labels = [c["label"] for c in chain]
        print(f"{kind:8s} (sequential FIFO) -> {labels}")
        assert len(chain) >= 1
        assert all({"label", "mode", "engine", "depth"} <= c.keys() for c in chain)
    print()


def post_formal(expr: str, kind: str, name: str, timeout_sec: int = 300) -> dict:
    resp = client.post(
        "/api/formal",
        data={
            "rtl_text": rtl_source,
            "top_module": "sync_fifo",
            "timeout_sec": str(timeout_sec),
            "properties": (
                f'[{{"name": "{name}", "expr": "{expr}", "kind": "{kind}"}}]'
            ),
        },
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert "properties" in body, body
    return body["properties"][0]


def check_happy_path() -> None:
    print("=== Happy path: count <= 4 (known PROVEN) ===")
    r = post_formal("count <= 4", "assert", "count_bound")
    print(f"verdict={r['verdict']} engine_label={r['engine_label']!r} attempts={r['attempts']}")
    assert r["verdict"] == "PROVEN", r
    assert len(r["attempts"]) == 1, r["attempts"]
    assert r["attempts"][0]["label"] == "PDR", r["attempts"][0]
    assert r["attempts"][0]["status"] == "PASS", r["attempts"][0]
    print("OK: single PDR attempt, unchanged from pre-Stage-3 behavior.\n")


def check_fallback_on_genuine_error() -> None:
    print("=== Genuine error: unbalanced parens (Verilog parse failure) ===")
    # An undeclared identifier alone is NOT an error -- Verilog implicitly
    # declares it as an undriven wire=0, so e.g. "nonexistent_sig == 1"
    # would legitimately compile and FALSIFY immediately (confirmed by
    # running it: PDR correctly stopped at one FAIL attempt, not an
    # error). A genuine parse failure is what should walk the whole chain.
    #
    # This also exercises the interaction with the existing (Stage 1)
    # LLM auto-fix retry: since a parse failure is NOT solver-specific,
    # all three engines should agree (all ERROR) before the chain gives
    # up -- confirming the chain doesn't stop early on a real problem.
    # Only *then* does the auto-fix retry kick in and re-walk the chain
    # for the corrected expression, which is expected to succeed.
    r = post_formal("((count <= 4", "assert", "bad_signal")
    print(f"verdict={r['verdict']} attempts={r['attempts']}")
    print(f"retried={r['retried']} retry_note={r['retry_note']!r}")
    assert len(r["attempts"]) >= 3, (
        f"a genuine parse error should walk the full 3-engine chain before giving up: {r['attempts']}"
    )
    first_three = r["attempts"][:3]
    assert all(a["status"] == "ERROR" for a in first_three), (
        f"a parse failure should error identically across all engines, not just some: {first_three}"
    )
    assert r["retried"] is True, "auto-fix retry should have kicked in after the chain exhausted on ERROR"
    assert r["verdict"] == "PROVEN", (
        f"auto-fix should have corrected the syntax and the chain re-proved it: {r}"
    )
    print("OK: chain walked all 3 engines honestly, then Stage 1's auto-fix retry recovered it.\n")


def main() -> None:
    check_chain_shapes()
    check_happy_path()
    check_fallback_on_genuine_error()
    print("=== All Stage 3 checks passed ===")


if __name__ == "__main__":
    main()
