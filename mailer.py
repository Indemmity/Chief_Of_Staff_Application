"""
mailer.py — puts generated reply drafts into your Gmail account, as you.

The final stage of the Chief-of-Staff pipeline:

    engine.fetch_threads() -> triage.triage_inbox()
    -> context_builder.assemble_context() -> draft_machine.draft_reply()
    -> mailer (this module)

Everything here goes through the Gmail MCP server (the same infrastructure
as engine.py), which authenticates with YOUR Google account via the OAuth
refresh token in ~/.gmail-mcp/credentials.json. Anything it creates or
sends therefore comes from your own email address and name — that is what
makes the reply "look like" you sent it. No address spoofing involved.

Two entry points:

    queue_reply(thread)  -> creates a Gmail DRAFT in your Drafts folder
                            (default, safe: you review and press Send)
    send_reply(thread)   -> sends the email immediately

Both accept the same thread dicts used everywhere else in the pipeline.
Pass `thread_id=` (from engine.fetch_threads()) to thread the reply under
the original Gmail conversation; without it the message goes out as a new
email that still comes from your address.

Run as a script (`python mailer.py`) to draft a reply to the sample Q3
Budget Review thread and drop it into your Gmail Drafts folder.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load the .env holding OPENROUTER_API_KEY / GEMINI_API_KEY before importing
# draft_machine/engine (llm_client.py also loads it on import; doing it here
# as well keeps keys available no matter which module is imported first).
WORKSPACE_DIR = Path(__file__).resolve().parent
load_dotenv(WORKSPACE_DIR / ".env")

import llm_client  # noqa: E402

from context_builder import SAMPLE_THREAD  # noqa: E402
from draft_machine import draft_reply_with_metadata  # noqa: E402

__all__ = [
    "extract_email_address",
    "build_reply_fields",
    "queue_reply",
    "send_reply",
]

# Pulls the addr-spec out of headers like "Priya Sharma <p@acme.com>".
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _ensure_utf8_output() -> None:
    """Windows consoles/redirects default to a legacy codepage (e.g. cp1252)
    while drafts can contain arbitrary Unicode."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def extract_email_address(from_header: str) -> str:
    """Return the bare address from a From header, e.g.
    'Priya Sharma <priya.sharma@acme.com>' -> 'priya.sharma@acme.com'."""
    match = _EMAIL_RE.search(from_header or "")
    return match.group(0) if match else ""


def build_reply_fields(thread: dict) -> dict:
    """
    Resolve who the reply goes to and what the subject should be.

    Recipient = sender of the thread's LAST message; subject = the thread
    subject prefixed with 'Re:' (unless it already starts with one).
    """
    messages = thread.get("messages") or []
    last_from = messages[-1].get("from", "") if messages else ""
    subject = (thread.get("subject") or "(no subject)").strip()
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    return {
        "reply_to_header": last_from or "(unknown sender)",
        "to": extract_email_address(last_from),
        "subject": subject,
    }


def _deliver(thread: dict, send: bool, thread_id: str | None) -> dict:
    """
    Generate the reply with the LLM backend chain (OpenRouter primary,
    Gemini fallback), then hand it to the Gmail MCP server as either a
    draft (`draft_email`) or an immediate send (`send_email`).

    Returns the draft metadata plus the delivery fields and the Gmail
    server's response text (e.g. the new draft/message ID).
    """
    # Imported lazily: engine pulls in the whole Gmail MCP stack, and the
    # Gmail client is only needed when we actually deliver.
    from engine import GmailMcpClient

    meta = draft_reply_with_metadata(thread)
    fields = build_reply_fields(thread)

    if not fields["to"]:
        raise RuntimeError(
            "Could not extract a recipient address from the last message "
            f"({fields['reply_to_header']!r}) — cannot deliver."
        )

    tool = "send_email" if send else "draft_email"
    args: dict[str, object] = {
        "to": [fields["to"]],  # schema expects a list of addresses
        "subject": fields["subject"],
        "body": meta["draft"],
    }
    if thread_id:
        # Keeps the reply inside the original Gmail conversation.
        args["threadId"] = thread_id

    with GmailMcpClient() as client:
        gmail_response = client.call_tool(tool, args)

    return {
        **meta,
        **fields,
        "action": tool,
        "thread_id": thread_id or "",
        "gmail_response": gmail_response.strip(),
    }


def queue_reply(thread: dict, thread_id: str | None = None) -> dict:
    """
    Generate a reply and create a Gmail DRAFT in your Drafts folder.

    Safe default: nothing is sent — open Gmail -> Drafts, review it, and
    press Send yourself.
    """
    return _deliver(thread, send=False, thread_id=thread_id)


def send_reply(thread: dict, thread_id: str | None = None) -> dict:
    """
    Generate a reply and SEND it immediately from your Gmail account.

    Use with care: the recipient receives a real email from your address.
    """
    return _deliver(thread, send=True, thread_id=thread_id)


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    _ensure_utf8_output()

    if not llm_client.configured_backends():
        print("ERROR: no LLM API key is set — draft generation needs one.")
        print()
        print("Fix: add a line to the .env file next to mailer.py:")
        print("    OPENROUTER_API_KEY=your-key-here   (primary — openrouter.ai/keys)")
        print("    GEMINI_API_KEY=your-key-here       (fallback — aistudio.google.com/apikey)")
        sys.exit(1)

    fields = build_reply_fields(SAMPLE_THREAD)

    rule = "=" * 70
    print(rule)
    print("MAILER — create the reply in your Gmail Drafts (nothing auto-sent)")
    print(rule)
    print()
    print(f"To:      {fields['to'] or '(could not parse recipient)'}")
    print(f"Subject: {fields['subject']}")
    print()
    print("Generating draft with the LLM backend (OpenRouter primary, Gemini")
    print("fallback), then creating it in your Drafts...")
    print()

    try:
        result = queue_reply(SAMPLE_THREAD)
    except RuntimeError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)

    print("-" * 70)
    print(result["draft"])
    print("-" * 70)
    print()
    print(f"Gmail says: {result['gmail_response']}")
    print()
    print("Open Gmail -> Drafts: the reply is there, from your account.")
    print("Review and press Send — or just delete it. Nothing was sent automatically.")
    print()

