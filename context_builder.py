"""
context_builder.py — assembles the full prompt context for the email
reply-drafting agent.

Given a thread dict and two style files on disk (`tone_profile.json` and
`past_replies.json`), `assemble_context()` returns the prompt pair a
drafting agent (e.g. Gemini) needs to write a reply that sounds like the
user actually wrote it:

    {
        "system": <persona + writing rules + few-shot examples>,
        "user":   <formatted thread history + drafting request>,
    }

The loaders resolve relative paths against this module's directory, so
`assemble_context(thread)` works from any working directory.

Run as a script (`python context_builder.py`), it builds the context for
a sample thread and prints both prompts for inspection.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "load_tone_profile",
    "load_past_replies",
    "format_thread_history",
    "build_system_prompt",
    "build_user_prompt",
    "assemble_context",
]

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #

WORKSPACE_DIR = Path(__file__).resolve().parent

# Cap on past replies shown as few-shot examples (spec: 2-3).
MAX_EXAMPLE_REPLIES = 3


def _ensure_utf8_output() -> None:
    """Windows consoles/redirects default to a legacy codepage (e.g. cp1252)
    while profiles, threads and prompts can contain arbitrary Unicode."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _resolve_path(path: str | Path) -> Path:
    """Resolve a data-file path against this module's directory when it is
    relative, so the loaders work regardless of the current working dir."""
    p = Path(path)
    return p if p.is_absolute() else WORKSPACE_DIR / p


def _read_json(path: str | Path) -> Any:
    """Read and parse a JSON file (relative paths resolve to this module's dir)."""
    return json.loads(_resolve_path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #


def load_tone_profile(path: str | Path = "tone_profile.json") -> dict:
    """Read and return the tone profile dict (persona, tone, quirks, ...)."""
    return _read_json(path)


def load_past_replies(path: str | Path = "past_replies.json") -> list:
    """Read and return the list of past reply examples."""
    return _read_json(path)


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def format_thread_history(thread: dict) -> str:
    """
    Format a thread dict as a readable, chronological transcript.

    `thread` looks like:

        {
            "subject": "Quick question",
            "messages": [
                {"from": "a@x.com", "date": "...", "body": "..."},
                ...
            ]
        }

    Returns a string showing who said what, in order.
    """
    lines = [f"Subject: {thread.get('subject', '(no subject)')}"]
    for i, message in enumerate(thread.get("messages", []), start=1):
        lines += [
            "",
            f"--- Message {i} ---",
            f"From: {message.get('from', '(unknown sender)')}",
            f"Date: {message.get('date', '(unknown date)')}",
            "",
            message.get("body", "").strip(),
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Prompt builders
# --------------------------------------------------------------------------- #


def _format_example(example: dict, index: int) -> str:
    """Format one past reply as a numbered few-shot example block."""
    return (
        f"--- Example {index} ---\n"
        f"To: {example.get('to', '(unknown recipient)')}\n"
        f"Subject: {example.get('subject', '(no subject)')}\n"
        f"\n"
        f"{example.get('body', '').strip()}"
    )


def build_system_prompt(tone_profile: dict, past_replies: list) -> str:
    """
    Build the system prompt for the drafting agent.

    Contains the persona (name, role, tone, formality), the writing rules
    taken from the tone profile's quirks list, and up to MAX_EXAMPLE_REPLIES
    past replies introduced by "Here's how {name} writes:".
    """
    name = tone_profile.get("name", "the user")
    role = tone_profile.get("role", "")
    tone = tone_profile.get("tone", "")
    formality = tone_profile.get("formality", "")

    sections: list[str] = [
        f"You are an AI assistant that drafts email replies on behalf of {name}, {role}.",
        (
            "PERSONA\n"
            f"- Name: {name}\n"
            f"- Role: {role}\n"
            f"- Tone: {tone}\n"
            f"- Formality: {formality}"
        ),
    ]

    quirks = tone_profile.get("quirks", [])
    if quirks:
        rules = "\n".join(f"- {quirk}" for quirk in quirks)
        sections.append(f"WRITING RULES\n{rules}")

    examples = "\n\n".join(
        _format_example(example, i)
        for i, example in enumerate(past_replies[:MAX_EXAMPLE_REPLIES], start=1)
    )
    if examples:
        sections.append(f"Here's how {name} writes:\n\n{examples}")

    sections.append(
        "When drafting, imitate the examples above — same voice, same length, "
        "same habits. Follow the writing rules exactly. Never mention that "
        "you are an AI."
    )

    return "\n\n".join(sections)


def build_user_prompt(thread_formatted: str) -> str:
    """Build the user message: the thread to reply to plus the drafting ask."""
    return (
        "Here is the email thread you are replying to:\n"
        "\n"
        f"{thread_formatted}\n"
        "\n"
        "Draft the reply. Output only the reply body, ready to send — no "
        "subject line, no placeholders, no explanations. Match the persona, "
        "writing rules, and examples from the system prompt."
    )


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #


def assemble_context(
    thread: dict,
    tone_path: str | Path = "tone_profile.json",
    replies_path: str | Path = "past_replies.json",
) -> dict:
    """
    Load the tone profile and past replies, format the thread, and return
    the full prompt context for the drafting agent:

        {"system": <system prompt>, "user": <user prompt>}
    """
    tone_profile = load_tone_profile(tone_path)
    past_replies = load_past_replies(replies_path)
    thread_formatted = format_thread_history(thread)
    return {
        "system": build_system_prompt(tone_profile, past_replies),
        "user": build_user_prompt(thread_formatted),
    }


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #

SAMPLE_THREAD = {
    "subject": "Q3 Budget Review — sign-off needed by Thursday",
    "messages": [
        {
            "from": "Meera Iyer <meera.iyer@acme.com>",
            "date": "Tue, 8 Sep 2026 10:05:12 +0000",
            "body": (
                "Hi Indemmity,\n"
                "\n"
                "Q3 budget is ready for your sign-off. Quick summary:\n"
                "\n"
                "1. Product tooling: $48k (up $6k — new analytics seat)\n"
                "2. Research panel: $22k (flat vs Q2)\n"
                "3. Launch events: $31k (down $9k, we cut the offsite)\n"
                "\n"
                "Everything is within the 5% variance threshold except the "
                "analytics seat. Can you confirm by Thursday so finance can "
                "lock the quarter?\n"
                "\n"
                "Thanks,\n"
                "Meera"
            ),
        },
        {
            "from": "Priya Sharma <priya.sharma@acme.com>",
            "date": "Tue, 8 Sep 2026 11:42:37 +0000",
            "body": (
                "Adding one flag — the analytics seat is the one my team needs "
                "for the launch dashboard. Without it we're back to weekly CSV "
                "exports, which slows every review cycle.\n"
                "\n"
                "Indemmity, can you confirm it's approved? The rest of Meera's "
                "list looks fine to me."
            ),
        },
    ],
}


if __name__ == "__main__":
    _ensure_utf8_output()

    context = assemble_context(SAMPLE_THREAD)

    rule = "=" * 70
    print(rule)
    print("ASSEMBLED PROMPT CONTEXT — reply drafting agent")
    print(rule)
    print()
    print("---------- SYSTEM ----------")
    print(context["system"])
    print()
    print("---------- USER ----------")
    print(context["user"])
    print()

