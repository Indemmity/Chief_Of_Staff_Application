"""
llm_client.py — the single gateway for every LLM call in the
Chief-of-Staff pipeline.

Backends, tried in order:

1. UnoRouter (primary) — OpenAI-compatible REST API at
   https://api.unorouter.com/v1, authenticated with UNOROUTER_API_KEY.

2. OpenRouter — OpenAI-compatible REST API at
   https://openrouter.ai/api/v1, authenticated with OPENROUTER_API_KEY.
   The model slug defaults to google/gemini-3.7-flash (the same model the
   codebase used via the Gemini SDK, now billed pay-as-you-go through
   OpenRouter) and can be overridden with OPENROUTER_MODEL in .env.

3. Google Gemini (fallback) — the classic google.generativeai SDK with
   GEMINI_API_KEY. Used automatically when OpenRouter is unavailable
   (missing key, network error, rate limit, empty response), so a drained
   OpenRouter quota never blocks the pipeline.

generate_text(system_prompt, user_prompt) returns the raw text reply no
matter which backend served it; llm_client.last_backend records which one
did (e.g. "openrouter:google/gemini-3.7-flash") so callers can surface it
in metadata. Both clients are created lazily on first use — importing this
module never touches the network and never requires a key to be present.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv

__all__ = [
    "OPENROUTER_BASE_URL",
    "OPENROUTER_MODEL",
    "UNOROUTER_BASE_URL",
    "UNOROUTER_MODEL",
    "GEMINI_MODEL",
    "DEFAULT_MAX_TOKENS",
    "last_backend",
    "generate_text",
    "configured_backends",
]

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #

WORKSPACE_DIR = Path(__file__).resolve().parent

# Load the .env holding OPENROUTER_API_KEY / GEMINI_API_KEY next to this
# script (already-set environment variables take precedence).
load_dotenv(WORKSPACE_DIR / ".env")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# UnoRouter exposes an OpenAI-compatible chat-completions endpoint.
UNOROUTER_BASE_URL = os.environ.get(
    "UNOROUTER_BASE_URL", "https://api.unorouter.com/v1"
)

# Free model from UnoRouter's platform quickstart; override in Secrets/.env.
UNOROUTER_MODEL = os.environ.get("UNOROUTER_MODEL", "gpt-oss-120b:free")

# OpenRouter model slug. Override with OPENROUTER_MODEL in .env — e.g. swap
# in a ":free" model or a newer Gemini flash release without touching code.
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-3.7-flash")

# Gemini fallback model, override with GEMINI_MODEL in .env.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")

# Completion cap sent when the caller doesn't pass max_tokens. Two failure
# modes have to be balanced here:
#   * A big cap  -> OpenRouter pre-authorises worst-case output spend and
#     rejects the request with a 402 when the account's credits can't cover
#     it (8192 was rejected with "can only afford 7842" at a near-zero
#     balance).
#   * A tiny cap -> Gemini reasoning models spend completion tokens on
#     thinking too, so the visible answer gets truncated mid-sentence.
# 4096 is enough for triage and drafting while fitting low OpenRouter credit
# balances that reject an 8192-token reservation.
# Override globally with LLM_MAX_TOKENS in .env (e.g. 2048 to fit a drained
# credit balance — triage replies need ~200 tokens, drafts a few hundred),
# or per call via the max_tokens argument.
DEFAULT_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "4096"))

# Which backend answered the most recent generate_text() call
# ("openrouter:<model>" or "gemini:<model>"); None until the first call.
last_backend: str | None = None

_openrouter_client = None  # OpenAI SDK client, created lazily on first use
_unorouter_client = None  # OpenAI SDK client, created lazily on first use


def configured_backends() -> list[str]:
    """Names of the backends that have an API key configured, in try order."""
    backends = []
    if os.environ.get("UNOROUTER_API_KEY"):
        backends.append("unorouter")
    if os.environ.get("OPENROUTER_API_KEY"):
        backends.append("openrouter")
    if os.environ.get("GEMINI_API_KEY"):
        backends.append("gemini")
    return backends


# --------------------------------------------------------------------------- #
# Backend implementations (each returns text or raises; an empty string counts
# as a failure so "blocked by safety filters" responses trigger the fallback)
# --------------------------------------------------------------------------- #


def _generate_unorouter(
    system_prompt: str, user_prompt: str, max_tokens: int | None
) -> str:
    """Call UnoRouter through its OpenAI-compatible chat API."""
    global _unorouter_client
    if _unorouter_client is None:
        from openai import OpenAI

        _unorouter_client = OpenAI(
            base_url=UNOROUTER_BASE_URL,
            api_key=os.environ["UNOROUTER_API_KEY"],
            default_headers={"X-Title": "Chief of Staff"},
        )

    messages = [{"role": "user", "content": user_prompt}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    kwargs: dict = {"model": UNOROUTER_MODEL, "messages": messages}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    response = _unorouter_client.chat.completions.create(**kwargs)
    if not response.choices:
        return ""
    return (response.choices[0].message.content or "").strip()


def _generate_openrouter(
    system_prompt: str, user_prompt: str, max_tokens: int | None
) -> str:
    """Call OpenRouter through the OpenAI-compatible chat completions API."""
    global _openrouter_client
    if _openrouter_client is None:
        from openai import OpenAI  # lazy: keeps `import llm_client` cheap

        _openrouter_client = OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=os.environ["OPENROUTER_API_KEY"],
            default_headers={
                # Optional OpenRouter attribution header (used for app
                # rankings on openrouter.ai; harmless otherwise).
                "X-Title": "Chief of Staff",
            },
        )

    messages = [{"role": "user", "content": user_prompt}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})

    kwargs: dict = {"model": OPENROUTER_MODEL, "messages": messages}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens

    response = _openrouter_client.chat.completions.create(**kwargs)
    if not response.choices:
        return ""
    return (response.choices[0].message.content or "").strip()


def _generate_gemini(
    system_prompt: str, user_prompt: str, max_tokens: int | None
) -> str:
    """Call Gemini through the classic google.generativeai SDK."""
    import warnings

    with warnings.catch_warnings():
        # The package raises a FutureWarning on import; silenced locally so
        # demo/test output stays clean (the trick draft_machine.py used).
        warnings.simplefilter("ignore", FutureWarning)
        import google.generativeai as genai

        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=system_prompt or None,
        )
        kwargs: dict = {}
        if max_tokens:
            kwargs["generation_config"] = {"max_output_tokens": max_tokens}
        response = model.generate_content(user_prompt, **kwargs)

    try:
        return (response.text or "").strip()
    except (ValueError, AttributeError, IndexError):
        return ""


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def generate_text(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int | None = None,
) -> str:
    """
    Generate a text reply: OpenRouter first, Gemini as the automatic
    fallback.

    `system_prompt` may be empty — triage.py passes one combined prompt as
    the user message.

    `max_tokens`: when omitted, DEFAULT_MAX_TOKENS is sent (4096 unless
    overridden with LLM_MAX_TOKENS in .env). Do not lower it much — Gemini
    reasoning models spend completion tokens on thinking too, so a small
    cap truncates the visible answer mid-sentence, while a big cap makes
    OpenRouter reject the request with a 402 when the account's credits
    can't cover the model's full output budget.

    Raises RuntimeError naming every backend that failed when none succeed;
    on success sets llm_client.last_backend.
    """
    global last_backend

    failures: list[str] = []
    backends = (
        ("unorouter", _generate_unorouter, "UNOROUTER_API_KEY"),
        ("openrouter", _generate_openrouter, "OPENROUTER_API_KEY"),
        ("gemini", _generate_gemini, "GEMINI_API_KEY"),
    )

    for name, call, key_name in backends:
        if not os.environ.get(key_name):
            failures.append(f"{name}: {key_name} is not set")
            continue
        try:
            try:
                text = call(system_prompt, user_prompt, max_tokens or DEFAULT_MAX_TOKENS)
            except Exception as exc:
                if "429" not in str(exc):
                    raise
                low = str(exc).lower()
                if "per-day" in low or "perday" in low:
                    # Daily quota exhausted — an 11s retry cannot help, and
                    # triaging 20 threads would otherwise sleep through
                    # minutes of pointless retries before failing. Fail fast;
                    # the next backend (and then the caller) decides.
                    raise
                # Transient rate limit (HTTP 429): the API names a short
                # retry window (~10s). Wait it out once before giving up —
                # manual testing triages several emails per minute, and a
                # 429 on the LAST backend would kill the whole run.
                time.sleep(11)
                text = call(system_prompt, user_prompt, max_tokens or DEFAULT_MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001 — any backend failure just means "try the next one"
            failures.append(f"{name}: {exc}")
            continue
        if not text:
            failures.append(f"{name}: empty response (blocked or truncated?)")
            continue

        model = {
            "unorouter": UNOROUTER_MODEL,
            "openrouter": OPENROUTER_MODEL,
            "gemini": GEMINI_MODEL,
        }[name]
        last_backend = f"{name}:{model}"
        return text

    raise RuntimeError(
        "All LLM backends failed:\n- " + "\n- ".join(failures)
    )
