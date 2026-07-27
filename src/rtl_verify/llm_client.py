"""Thin wrapper around the Anthropic API for property suggestion/conversion.

Reads ANTHROPIC_API_KEY from the environment (.env via python-dotenv, or a
real env var) — never hardcode a key, never accept one as a request
parameter from a client. The API import is local to complete() so the
rest of the app works fine when the package or key isn't present.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

DEFAULT_MODEL = "claude-sonnet-5"


class LLMNotConfigured(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _require_configured() -> None:
    if not is_configured():
        raise LLMNotConfigured(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key "
            "from https://console.anthropic.com/settings/keys"
        )


def complete(system: str, user: str, max_tokens: int = 2000, model: str = DEFAULT_MODEL) -> str:
    """One-shot text completion. Raises LLMNotConfigured if no key is set."""
    _require_configured()
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )


def complete_structured(
    system: str,
    user: str,
    schema: dict,
    max_tokens: int = 4000,
    model: str = DEFAULT_MODEL,
) -> dict:
    """One-shot completion constrained to a JSON schema — no manual parsing of
    free-form text, no risk of the model wrapping JSON in prose or code fences.
    Raises LLMNotConfigured if no key is set; raises json.JSONDecodeError only
    if the API itself returns something that fails to parse (shouldn't happen
    under a schema, but never trust that blindly).
    """
    _require_configured()
    import json

    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text = next(block.text for block in resp.content if getattr(block, "type", "") == "text")
    return json.loads(text)
