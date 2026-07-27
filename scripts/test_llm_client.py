"""Smoke test: confirm ANTHROPIC_API_KEY is set and the API is reachable."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.llm_client import LLMNotConfigured, complete, is_configured  # noqa: E402


def main() -> None:
    print("configured:", is_configured())
    if not is_configured():
        print("Set ANTHROPIC_API_KEY in .env (see .env.example) and re-run.")
        return
    try:
        reply = complete(
            system="Reply with exactly one word.",
            user="Say 'hello'.",
            max_tokens=20,
        )
        print("reply:", repr(reply))
    except LLMNotConfigured as e:
        print("ERROR:", e)


if __name__ == "__main__":
    main()
