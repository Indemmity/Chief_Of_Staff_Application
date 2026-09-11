"""
bootstrap.py — cloud-runtime credential plumbing for The Draft Desk.

Locally, every credential lives in a file that is deliberately NOT in the
git repo:

    .env                            OPENROUTER_API_KEY / GEMINI_API_KEY, …
    token.json                      the cached Google OAuth grant that
                                    engine.send_reply() / calendar_engine
                                    (and engine's raw-API fetch fallback) use
    ~/.gmail-mcp/credentials.json   the Gmail MCP server's own OAuth token

A fresh deployment (e.g. Streamlit Community Cloud) clones only the repo, so
none of those files exist there — and a headless box can never run the
browser consent flow to create them. Streamlit Cloud delivers configuration
through its Secrets UI instead, which surfaces as st.secrets.

ensure_runtime_credentials() bridges the two worlds, idempotently, on every
app start:

1. SECRETS -> ENVIRONMENT. Every flat (scalar) key in st.secrets becomes an
   environment variable via setdefault, so a local .env still wins when both
   exist. After this, llm_client's os.environ lookups work unchanged on the
   cloud.
2. GOOGLE_TOKEN_JSON -> token.json. When the secret is set and token.json is
   missing, the secret's JSON is written to token.json. The cached grant
   carries its own refresh token, so no browser consent is ever needed;
   expired grants are refreshed silently by the _build_*_service() helpers.
3. GMAIL_MCP_CREDENTIALS_JSON -> ~/.gmail-mcp/credentials.json. Same pattern
   for the Gmail MCP server's own token, for deployments that do provide an
   MCP server.

Everything is a no-op on a normal dev machine (files exist, st.secrets
absent), so calling it unconditionally is safe.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

WORKSPACE_DIR = Path(__file__).resolve().parent

__all__ = ["ensure_runtime_credentials"]


def _scalar_secrets() -> dict[str, str]:
    """Flat {name: string-value} view of st.secrets ({} when unavailable)."""
    try:
        import streamlit as st

        raw = dict(st.secrets)
    except Exception:  # noqa: BLE001 — no secrets file / outside Streamlit
        return {}
    flat: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            continue  # sections are for st.sections access, not env vars
        flat[str(key)] = str(value)
    return flat


def _materialize_json_secret(secret_name: str, target: Path) -> str:
    """
    Write a JSON-valued secret to `target` when the file does not exist yet.

    Returns a short report line. Never raises for a malformed secret: the
    file is simply not written, and the credential consumers raise their own
    precise errors if the file is unusable.
    """
    value = os.environ.get(secret_name, "")
    if not value:
        return f"{target.name}: {secret_name} not set — skipped"
    if target.exists():
        return f"{target.name}: already present — kept"
    try:
        parsed = json.loads(value)
    except ValueError:
        return f"{target.name}: {secret_name} is not valid JSON — NOT written"
    if not isinstance(parsed, dict):
        return f"{target.name}: {secret_name} is not a JSON object — NOT written"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    return f"{target.name}: written from {secret_name}"


def ensure_runtime_credentials() -> list[str]:
    """
    Apply the secrets -> environment/file bridges (module docstring).

    Idempotent; returns human-readable report lines for logging.
    """
    report: list[str] = []

    # 1) Secrets -> environment (never overrides already-set variables).
    applied = 0
    for key, value in _scalar_secrets().items():
        if not os.environ.get(key):
            os.environ[key] = value
            applied += 1
    report.append(f"environment: {applied} secret(s) applied")

    # 2) The Google OAuth grant -> token.json (headless raw-API auth).
    report.append(
        _materialize_json_secret("GOOGLE_TOKEN_JSON", WORKSPACE_DIR / "token.json")
    )

    # 3) The Gmail MCP server's own grant (only relevant when the deployment
    #    actually provides an MCP server).
    mcp_credentials = Path(
        os.environ.get(
            "GMAIL_MCP_CREDENTIALS_PATH",
            str(Path.home() / ".gmail-mcp" / "credentials.json"),
        )
    )
    report.append(
        _materialize_json_secret("GMAIL_MCP_CREDENTIALS_JSON", mcp_credentials)
    )

    return report
