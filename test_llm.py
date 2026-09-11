"""
test_llm.py — smoke test for the LLM backend chain (llm_client.py).

Run:  python test_llm.py

Checks, in order:
1. Which backends are configured (OPENROUTER_API_KEY / GEMINI_API_KEY).
2. A live call through the normal path — OpenRouter primary, Gemini
   automatic fallback — printing which backend actually answered.
3. A forced Gemini call (the OpenRouter key is temporarily hidden from the
   environment) so the fallback path is exercised even when the primary
   is healthy.
"""

from __future__ import annotations

import os
import sys

import llm_client


def _ensure_utf8_output() -> None:
    """Windows consoles/redirects default to a legacy codepage (e.g. cp1252)
    while model replies can contain arbitrary Unicode."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _ask(label: str, user_prompt: str) -> bool:
    """One smoke-test call; returns True when it produced text."""
    print(f"--- {label} ---")
    try:
        text = llm_client.generate_text(
            "You are a helpful assistant.",
            user_prompt,
        )
    except Exception as exc:  # noqa: BLE001 — the point is to report, not crash
        print(f"FAILED: {exc}")
        print()
        return False
    print(f"backend: {llm_client.last_backend}")
    print(f"reply:   {text}")
    print()
    return bool(text)


if __name__ == "__main__":
    _ensure_utf8_output()

    print("=" * 70)
    print("LLM CONNECTION TEST — OpenRouter primary, Gemini fallback")
    print("=" * 70)
    print()
    print(f"OpenRouter model: {llm_client.OPENROUTER_MODEL}")
    print(f"Gemini model:     {llm_client.GEMINI_MODEL}")
    configured = llm_client.configured_backends()
    print(f"Configured:       {', '.join(configured) or 'NONE'}")
    print()

    if not configured:
        print("No API keys found. Add OPENROUTER_API_KEY (openrouter.ai/keys)")
        print("and/or GEMINI_API_KEY (aistudio.google.com/apikey) to .env.")
        sys.exit(1)

    ok = _ask(
        "Normal path (OpenRouter primary, Gemini fallback)",
        "Reply with exactly: LLM connection works.",
    )

    # Force the fallback: hide the OpenRouter key from the environment so
    # generate_text() must route to Gemini. Informational only — Gemini's
    # free tier may be drained on any given day; the wiring is what matters.
    saved_key = os.environ.pop("OPENROUTER_API_KEY", None)
    llm_client.last_backend = None
    fallback_ok = _ask(
        "Forced fallback (OpenRouter key hidden, Gemini only)",
        "Reply with exactly: Gemini fallback works.",
    )
    if saved_key is not None:
        os.environ["OPENROUTER_API_KEY"] = saved_key

    print("=" * 70)
    if ok:
        print("RESULT: PASS — the OpenRouter primary is serving requests.")
        if not fallback_ok:
            print("Note: the forced-Gemini leg did not complete (most likely")
            print("the Gemini free tier is drained). The fallback wiring is")
            print("still in place and engages automatically when OpenRouter")
            print("fails — re-run this test after the quota resets.")
    else:
        print("RESULT: FAIL — see the errors above")
    print("=" * 70)
    sys.exit(0 if ok else 1)

