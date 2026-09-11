"""Offline smoke test for triage.parse_triage_response hardening.

No LLM/network: feeds the parser every reply shape free-tier models throw
at it and asserts the eight fields survive each one.

Run:  .venv\\Scripts\\python.exe _triage_parse_smoke_test.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

import triage

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def check(name: str, text: str, want: dict) -> None:
    got = triage.parse_triage_response(text)
    for key, expected in want.items():
        actual = got.get(key)
        assert actual == expected, f"{name}: {key} = {actual!r}, want {expected!r}\ntext: {text!r}"
    print(f"PASS {name}")


CANONICAL = (
    "Priority: urgent\n"
    "Category: meeting-request\n"
    "Action: reply\n"
    "Needs Reply: yes\n"
    "Deadline: tomorrow\n"
    "Confidence: high\n"
    "Reason: explicit ask\n"
    "Why It Matters: time sensitive"
)

# 1. The canonical format still parses.
check("canonical", CANONICAL, {"priority": "urgent", "category": "meeting-request"})

# 2. Markdown bold + code fences + bullets (what free models like to emit).
check(
    "bold/fenced/bulleted",
    "```text\n"
    "- **Priority:** needs-reply\n"
    "- **Category:** job-opportunity\n"
    "- **Action:** reply\n"
    "- **Needs Reply:** yes\n"
    "- **Deadline:** Friday\n"
    "- **Confidence:** medium\n"
    "- **Reason:** recruiter ask\n"
    "- **Why It Matters:** career move\n"
    "```",
    {"priority": "needs-reply", "category": "job-opportunity", "confidence": "medium"},
)

# 3. JSON object reply.
check(
    "json",
    'Sure! Here is the triage:\n{"priority": "spam", "category": "promotional", '
    '"action": "delete", "needs_reply": "no", "deadline": "none", '
    '"confidence": "high", "reason": "ads", "why_it_matters": "none"}',
    {"priority": "spam", "category": "promotional", "action": "delete"},
)

# 4. "label - value" dashes and em-dashes, case-insensitive labels.
check(
    "dash labels",
    "priority — important\ncategory: Follow-Up\nACTION - reply\n"
    "needs reply: no\ndeadline: Sep 12\nconfidence: high\n"
    "Reason: newsletter\nWhy It Matters: low",
    {"priority": "important", "category": "follow-up", "action": "reply"},
)

# 5. Thinking preamble before the labels (reasoning models).
check(
    "preamble",
    "Let me analyze this email. The sender asks for immediate action.\n\n"
    + CANONICAL,
    {"priority": "urgent", "action": "reply"},
)

# 6. Garbage / empty still fall back to safe defaults (no crash).
garbage = triage.parse_triage_response("The model replied in Chinese: 优先级 高")
assert garbage["priority"] == "unknown", garbage
assert triage.parse_triage_response("")["priority"] == "unknown"
assert triage.parse_triage_response("")["category"] == "other"
print("PASS garbage/empty fall back to safe defaults")

print()
print("All parse_triage_response smoke checks passed.")
