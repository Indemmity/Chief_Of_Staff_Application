"""
task_logger.py — append-only audit trail for the actions the Approval Gate
takes outside the app (emails sent, meetings booked).

Three functions:

    log_action(action_type, thread_subject, detail, action_id)
        Appends one record to action_log.json and returns it.
        action_type is "sent" (detail = recipient email, action_id = Gmail
        message_id) or "booked" (detail = meeting title, action_id = Google
        Calendar event_id).

    get_action_log() -> list[dict]
        The full list; [] when the file does not exist or is empty.

    clear_log() -> None
        Writes an empty list to action_log.json.

Every record looks like:

    {
        "timestamp": "2026-09-10T14:32:07",
        "action_type": "sent",
        "thread_subject": "Q3 Budget Review — sign-off needed by Thursday",
        "detail": "meera.iyer@acme.com",
        "id": "1948f2c0a9d3e11b"
    }

Standard library only (json + datetime, via pathlib for the path) — no LLM,
no network, safe to import from anywhere.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

WORKSPACE_DIR = Path(__file__).resolve().parent
LOG_PATH = WORKSPACE_DIR / "action_log.json"

__all__ = ["log_action", "get_action_log", "clear_log"]


def log_action(action_type: str, thread_subject: str, detail: str, action_id: str) -> dict:
    """Append one action record to action_log.json and return it.

    action_type must be "sent" or "booked" — anything else raises ValueError
    (a typo'd action type would corrupt the meaning of the audit trail).
    The file is created on first write; existing records are preserved.
    """
    if action_type not in ("sent", "booked"):
        raise ValueError(
            f"action_type must be 'sent' or 'booked', got {action_type!r}"
        )

    records = get_action_log()
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "action_type": action_type,
        "thread_subject": thread_subject,
        "detail": detail,
        "id": action_id,
    }
    records.append(record)
    LOG_PATH.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return record


def get_action_log() -> list[dict]:
    """Return every record in action_log.json (oldest first).

    [] when the file does not exist or is empty. A file that exists but is
    NOT valid JSON raises json.JSONDecodeError instead of returning [] —
    silently swallowing a corrupt audit log would let the next log_action()
    overwrite it with a single record, destroying the history.
    """
    if not LOG_PATH.exists():
        return []
    text = LOG_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return []
    records = json.loads(text)
    return records if isinstance(records, list) else []


def clear_log() -> None:
    """Reset the log: action_log.json becomes an empty list."""
    LOG_PATH.write_text("[]", encoding="utf-8")
