"""
draft_machine.py — generates email reply drafts with the LLM backend
(OpenRouter primary, Gemini automatic fallback — see llm_client.py).

The drafting stage of the Chief-of-Staff pipeline: engine.fetch_threads()
-> triage.triage_inbox() -> context_builder.assemble_context() -> here.

`draft_reply(thread)` assembles the persona + few-shot prompt context via
context_builder, layers on the drafting rules (ONE-ASK RULE, length
control, no AI filler, clear structure), calls the backend chain, and
returns ONLY the reply text ready to send.
`draft_reply_with_metadata(thread)` returns the same draft plus the
backend that served it, thread subject, and who we're replying to.

Run as a script (`python draft_machine.py`) to draft a reply to the
sample Q3 Budget Review thread defined in context_builder.py.
"""

from __future__ import annotations

import sys

import llm_client
from llm_client import generate_text

from context_builder import SAMPLE_THREAD, assemble_context

__all__ = ["MODEL", "DRAFTING_RULES", "SAMPLE_THREADS", "draft_reply", "draft_reply_with_metadata"]

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #

# Default OpenRouter model slug (override with OPENROUTER_MODEL in .env).
# google/gemini-3.7-flash is the same model the Gemini-SDK version of this
# module called, now billed pay-as-you-go through OpenRouter. Kept as a
# module constant because approval_gate.py and mailer.py import it for
# display; the backend that actually served each draft is recorded by
# llm_client and surfaces in draft_reply_with_metadata()'s "model" field.
MODEL = llm_client.OPENROUTER_MODEL

DRAFTING_RULES = """\
DRAFTING RULES

1. ONE-ASK RULE — Every email makes exactly ONE clear ask, or answers
   exactly ONE question. Never bundle multiple requests into a single
   email. If the thread raises several points, address them in the body
   but end on the single thing that matters.

2. LENGTH CONTROL — Match the energy of the thread: short thread, short
   reply. Hard cap of 5 sentences. Use numbered points when listing more
   than two things.

3. NO AI FILLER — Never open with or use phrases like "I hope this finds
   you well", "Thank you for reaching out", "I wanted to follow up",
   "Please don't hesitate to reach out". Start with the substance.

4. STRUCTURE — In this order: acknowledge briefly, give the response,
   end with ONE clear next step."""


# --------------------------------------------------------------------------- #
# Sample threads (demo inbox for approval_gate.py)
# --------------------------------------------------------------------------- #

# context_builder.SAMPLE_THREAD plus two more demo threads, so the approval
# gate has a small pick-list to offer. Same schema: subject + messages with
# from/date/body. The LAST message in each list is the one being replied to.

_VENDOR_THREAD = {
    "subject": "Re: Vendorly rollout — go-live date & 500-seat quote",
    "messages": [
        {
            "from": "Sam Chen <sam.chen@vendorly.io>",
            "date": "Mon, 7 Sep 2026 09:18:44 +0000",
            "body": (
                "Hi Indemmity,\n"
                "\n"
                "Following up on Friday's call — two things before we can "
                "move to contracting:\n"
                "\n"
                "1. What go-live date should we plan around?\n"
                "2. Can you confirm the final seat count so I can reissue "
                "the 500-tier quote?\n"
                "\n"
                "If I have both by Wednesday, the paperwork lands this week.\n"
                "\n"
                "Best,\n"
                "Sam"
            ),
        },
        {
            "from": "Sam Chen <sam.chen@vendorly.io>",
            "date": "Tue, 8 Sep 2026 08:55:10 +0000",
            "body": (
                "Gentle nudge on the two items below — procurement needs "
                "the confirmed numbers by Thursday to hold the Q4 slot."
            ),
        },
    ],
}

_SPEC_FEEDBACK_THREAD = {
    "subject": "Re: Feedback on the search-relevance spec?",
    "messages": [
        {
            "from": "Arjun K <arjun.k@acme.com>",
            "date": "Mon, 7 Sep 2026 15:37:02 +0000",
            "body": (
                "Indemmity — the search-relevance spec is in Notion and ready "
                "for your pass:\n"
                "\n"
                "notion.acme.com/search-relevance-spec\n"
                "\n"
                "The one open question is §3 (ranking signals) — everything "
                "else I think is settled. Need your call by EOD tomorrow so "
                "eng can start Monday."
            ),
        },
    ],
}

SAMPLE_THREADS = [SAMPLE_THREAD, _VENDOR_THREAD, _SPEC_FEEDBACK_THREAD]


def _ensure_utf8_output() -> None:
    """Windows consoles/redirects default to a legacy codepage (e.g. cp1252)
    while drafts can contain arbitrary Unicode."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _generate_draft(thread: dict) -> str:
    """
    Assemble the prompt context for `thread`, layer on the drafting rules,
    call the LLM backend chain (OpenRouter primary, Gemini fallback), and
    return the raw draft text.

    Raises RuntimeError with an actionable message when no API key is
    configured or every backend fails or returns no text.
    """
    # Persona + few-shot examples (system) and the thread (user) from
    # context_builder, with the drafting rules appended to the system side.
    context = assemble_context(thread)
    system_prompt = f"{context['system']}\n\n{DRAFTING_RULES}"

    # No max_tokens cap: Gemini reasoning models spend completion tokens on
    # thinking too, so a small cap truncates the visible draft mid-sentence.
    return generate_text(
        system_prompt=system_prompt,
        user_prompt=context["user"],
    )


def draft_reply_with_metadata(thread: dict) -> dict:
    """
    Draft a reply to `thread` and return a dict with the draft plus metadata:

        {
            "draft":    <reply text, ready to send>,
            "model":    <backend + model that answered, e.g.
                         "openrouter:google/gemini-3.7-flash">,
            "subject":  <thread subject>,
            "reply_to": <sender of the last message — who we're replying to>,
        }
    """
    draft = _generate_draft(thread)

    messages = thread.get("messages") or []
    reply_to = (
        messages[-1].get("from", "(unknown sender)") if messages else "(unknown sender)"
    )

    return {
        "draft": draft,
        "model": llm_client.last_backend or MODEL,
        "subject": thread.get("subject", "(no subject)"),
        "reply_to": reply_to,
    }


def draft_reply(thread: dict) -> str:
    """
    Draft a reply to `thread` and return ONLY the draft text — no subject
    line, no explanation.
    """
    return draft_reply_with_metadata(thread)["draft"]


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    _ensure_utf8_output()

    if not llm_client.configured_backends():
        print("ERROR: no LLM API key is set — cannot draft.")
        print()
        print("Fix: add a line to the .env file next to llm_client.py:")
        print("    OPENROUTER_API_KEY=your-key-here   (primary — openrouter.ai/keys)")
        print("    GEMINI_API_KEY=your-key-here       (fallback — aistudio.google.com/apikey)")
        sys.exit(1)

    rule = "=" * 70
    print(rule)
    print(f"DRAFT MACHINE — {MODEL}")
    print(rule)
    print()
    print(f"Thread subject: {SAMPLE_THREAD['subject']}")
    print("Drafting reply with the LLM backend (OpenRouter primary, Gemini fallback)...")
    print()

    meta = draft_reply_with_metadata(SAMPLE_THREAD)

    print("-" * 70)
    print(f"To:      {meta['reply_to']}")
    print(f"Model:   {meta['model']}")
    print(f"Subject: {meta['subject']}")
    print("-" * 70)
    print()
    print(meta["draft"])
    print()
    print("-" * 70)
    print(f"Draft length: {len(meta['draft'])} chars | draft_reply() returns exactly this text")

