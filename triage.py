"""
triage.py — classifies inbox threads (priority, category, action, deadline,
confidence) with the LLM backend from llm_client: OpenRouter primary,
Gemini automatic fallback.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor

from llm_client import generate_text


def triage_thread(sender: str, subject: str, snippet: str) -> dict:
    prompt = f"""
You are an intelligent Chief of Staff AI helping a user manage their inbox.

Your job is to analyze an email and determine its importance,
whether the user needs to respond, and what action should be taken.

Email information:

Sender: {sender}
Subject: {subject}
Preview: {snippet}


PRIORITY

Choose exactly one:

- urgent
- needs-reply
- important
- fyi
- low-priority
- spam


CATEGORY

Choose exactly one:

- meeting-request
- task-request
- follow-up
- job-opportunity
- newsletter
- promotional
- billing
- social
- administrative
- spam
- other


ACTION

Choose exactly one:

- reply
- draft-reply
- read
- archive
- flag
- delete


NEEDS REPLY

Choose exactly one:

- yes
- no


DEADLINE

Extract a deadline if one is explicitly mentioned.

Examples:

- today 5 PM
- Friday afternoon
- September 10
- tomorrow
- none

If no deadline is mentioned, write:

none


CONFIDENCE

Choose exactly one:

- high
- medium
- low


ACTION RULES

- Use "reply" when the sender clearly expects a response.
- Use "draft-reply" when a response is needed but the user should review it before sending.
- Use "read" when the information is useful and requires the user's attention.
- Use "archive" when the email is informational but does not need to remain in the inbox.
- Use "flag" when the email is important and should remain visible for follow-up.
- Use "delete" for obvious promotional emails or spam that provide no meaningful value.

IMPORTANT SAFETY RULE:

Never recommend delete for important work, financial, legal,
personal, security, or account-related emails unless the email
is clearly spam.

A legitimate newsletter is NOT automatically spam.

A promotional email is NOT automatically spam.

Use "newsletter" for recurring informational content.

Use "promotional" for legitimate marketing, sales, discounts,
offers, and advertisements.

Use "spam" only when the email appears unsolicited, deceptive,
suspicious, malicious, or clearly unwanted.


PRIORITY RULES

- urgent:
  Immediate action is required or there is a very close deadline.

- needs-reply:
  The sender expects a response but the situation is not necessarily urgent.

- important:
  The information is important but does not necessarily require an immediate response.

- fyi:
  Useful information that does not require action.

- low-priority:
  Little urgency or importance, such as routine promotions.

- spam:
  Clearly suspicious, deceptive, or unwanted email.


RESPOND IN EXACTLY THIS FORMAT:

Priority: <value>
Category: <value>
Action: <value>
Needs Reply: <yes | no>
Deadline: <date/time or none>
Confidence: <high | medium | low>
Reason: <one sentence explaining the classification>
Why It Matters: <one sentence explaining why this email matters to the user>

Do not add extra fields.
Do not use markdown.
Do not use bullet points.
"""


    response_text = generate_text(
        system_prompt="",  # the triage prompt is one combined message
        user_prompt=prompt,
    )

    return parse_triage_response(response_text)


def parse_triage_response(text: str) -> dict:
    """Extract the eight triage fields from an LLM reply.

    Tolerates every wrapper LLMs like to add — code fences, markdown bold
    ("**Priority:**"), leading bullets, "label - value" dashes, and JSON
    objects — because a free-tier model that replies in any of those shapes
    used to fall through to priority "unknown" for the entire inbox.
    """
    result = {
        "priority": "unknown",
        "category": "other",
        "action": "read",
        "needs_reply": "unknown",
        "deadline": "none",
        "confidence": "low",
        "reason": "",
        "why_it_matters": ""
    }
    if not text:
        return result

    # Known values stay lowercase; free-text fields keep their casing.
    LOWER_KEYS = {"priority", "category", "action", "needs_reply", "confidence"}
    labels = {
        "priority": "priority",
        "category": "category",
        "action": "action",
        "needs reply": "needs_reply",
        "needs_reply": "needs_reply",
        "deadline": "deadline",
        "confidence": "confidence",
        "reason": "reason",
        "why it matters": "why_it_matters",
        "why_it_matters": "why_it_matters",
    }

    def find(label: str) -> str | None:
        # Handles "Priority: urgent", "**Priority:** urgent", "- priority: urgent",
        # "1. Priority: urgent", "Priority - urgent" / em-dash, case-insensitive.
        line = re.search(
            rf"(?im)^\s*(?:[-*\u2022]|\d+[.)])?\s*\**\s*{re.escape(label)}\s*\**\s*[:\-\u2014]\s*(.+?)\s*$",
            text,
        )
        if line:
            # A bold-close between the colon and the value ("**Priority:**
            # needs-reply") leaves "** " in the capture — strip the wrappers.
            value = line.group(1).strip().strip("`* \t").strip()
            return value or None
        # JSON shape: "priority": "urgent"
        as_json = re.search(
            rf'"{re.escape(label)}"\s*:\s*"([^"]+)"', text, re.IGNORECASE
        )
        if as_json:
            return as_json.group(1).strip()
        return None

    for label, key in labels.items():
        value = find(label)
        if value:
            result[key] = value.lower() if key in LOWER_KEYS else value

    return result


def triage_inbox(threads: list) -> list:
    if not threads:
        return []

    # Inbox classification is I/O-bound: each email is an independent model
    # request. A small bounded pool cuts wall-clock time while avoiding the
    # rate-limit spikes caused by launching one worker per email.
    default_workers = min(4, len(threads))
    max_workers = max(
        1, int(os.environ.get("LLM_CONCURRENCY", str(default_workers)))
    )
    max_workers = min(max_workers, len(threads))

    def classify(thread: dict) -> dict:
        label = triage_thread(
            sender=thread["sender"],
            subject=thread["subject"],
            snippet=thread["snippet"],
        )
        return {**thread, **label}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        triaged = list(pool.map(classify, threads))

    priority_order = {
        "urgent": 0,
        "needs-reply": 1,
        "important": 2,
        "fyi": 3,
        "low-priority": 4,
        "spam": 5,
        "unknown": 6
    }

    triaged.sort(
        key=lambda x: priority_order.get(
            x["priority"], 6
        )
    )

    return triaged


# ============================================================
# SAMPLE EMAILS FOR TESTING
# ============================================================

sample_threads = [

    {
        "sender": "boss@company.com",
        "subject": "Need your input by EOD",
        "snippet": "Can you review the attached proposal before 5pm?"
    },

    {
        "sender": "newsletter@medium.com",
        "subject": "Top stories for you this week",
        "snippet": "Here's what's trending in technology and startups..."
    },

    {
        "sender": "recruiter@startup.io",
        "subject": "Quick call this week?",
        "snippet": "Hi, I came across your profile and wanted to connect about an opportunity."
    },

    {
        "sender": "marketing@shopping.com",
        "subject": "50% OFF! Limited time offer!",
        "snippet": "Shop now and get an exclusive discount before midnight!"
    },

    {
        "sender": "client@company.com",
        "subject": "Project update needed by Friday",
        "snippet": "Could you send the revised project status by Friday afternoon?"
    },

    {
        "sender": "winner@unknown-domain.com",
        "subject": "Congratulations! You have won $50,000!",
        "snippet": "Claim your prize now! Click this link immediately to receive your money."
    }
]


# ============================================================
# RUN TRIAGE
# ============================================================

if __name__ == "__main__":
    # Demo run: triage the sample threads above when this file is executed
    # directly. Guarded so that `from triage import triage_inbox` (e.g. in
    # engine.py) does not trigger LLM calls on import.
    results = triage_inbox(sample_threads)

    print("\n")
    print("=" * 70)
    print("           CHIEF OF STAFF — EMAIL TRIAGE")
    print("=" * 70)
    print()

    for r in results:
        print(f"[{r['priority'].upper()}] [{r['category'].upper()}]")
        print(f"Subject:       {r['subject']}")
        print(f"Action:        {r['action']}")
        print(f"Needs Reply:   {r['needs_reply']}")
        print(f"Deadline:      {r['deadline']}")
        print(f"Confidence:    {r['confidence']}")
        print(f"Reason:        {r['reason']}")
        print(f"Why It Matters:{r['why_it_matters']}")
        print("-" * 70)
