"""
calendar_engine.py — Google Calendar access for the Chief-of-Staff pipeline.

Mirrors engine.py's raw-API side (_build_gmail_service): same OAuth flow,
same client secrets (~/.gmail-mcp/gcp-oauth.keys.json), same cached grant
(token.json next to this file), same three scopes — but builds a Calendar
v3 service instead of Gmail v1. Because the grant is shared, no extra
browser consent is needed beyond what engine.py already asks for: the
calendar permission is part of the same three-scope grant.

Importing this module applies the IPv4 monkey-patch (identical to
engine.py): some networks hand out unreachable AAAA records for
googleapis.com, which shows up in googleapiclient as long timeouts or
connection errors; forcing AF_INET avoids them.

The Google libraries are imported lazily inside the builder, so merely
importing this module never touches the network and never requires the
Google packages to be installed.

parse_meeting_request(thread) is the first consumer of the grant: it asks
Gemini (gemini-2.5-flash) to pull meeting details — proposed times,
attendees, topic, duration — out of a thread and returns them as a plain
dict, or {"parsing_error": ..., "raw": ...} if anything fails, so callers
never need a try/except.

check_availability(time_min, time_max) and find_free_slot(proposed_times,
duration_minutes) close the loop: proposed times are verified against the
primary calendar's FreeBusy view, and the first open slot is returned. Both
fail closed — any problem counts as "busy" — and last_availability_error()
explains why the last check said so, so a broken check (auth, quota, the
Calendar API being disabled on the OAuth project) is never mistaken for a
genuinely full calendar.

create_event(summary, start_time, duration_minutes, attendees,
description="") finishes the flow: it inserts the event on the primary
calendar with sendUpdates="all" (attendees get invitation emails) and
returns the created event dict from the API.
"""

from __future__ import annotations

import json
import os
import re
import socket
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# IPv4 monkey-patch (identical to engine.py)
# --------------------------------------------------------------------------- #

# Some networks resolve googleapis.com to unreachable IPv6 addresses, which
# surfaces in googleapiclient as long timeouts / connection errors. Forcing
# AF_INET makes every socket opened by this process IPv4-only.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

# --------------------------------------------------------------------------- #
# Shared OAuth material (single source of truth: engine.py)
# --------------------------------------------------------------------------- #

# SCOPES is engine.GMAIL_SCOPES — the exact same three scopes (gmail.readonly,
# gmail.send, calendar) that _build_gmail_service() consents to, imported
# rather than re-declared so the two files can never drift apart. token.json
# is shared the same way: one cached grant serves both services.
from engine import GMAIL_SCOPES as SCOPES  # noqa: E402
from engine import WORKSPACE_DIR  # noqa: E402

# GEMINI_API_KEY for parse_meeting_request(): engine's import chain already
# loads the .env (via llm_client), but load it here as well so this module
# works no matter which module is imported first (mailer.py's convention).
from dotenv import load_dotenv  # noqa: E402

load_dotenv(WORKSPACE_DIR / ".env")

__all__ = [
    "SCOPES",
    "_build_calendar_service",
    "parse_meeting_request",
    "check_availability",
    "find_free_slot",
    "last_availability_error",
    "create_event",
]


def _build_calendar_service() -> Any:
    """
    Build a Calendar v3 Google API service object (googleapiclient).

    Exact same OAuth pattern as engine._build_gmail_service(): the cached
    grant in token.json (next to this file) is reused and refreshed
    silently; only when it is missing does the browser ask for the shared
    three scopes (gmail.readonly, gmail.send, calendar — the calendar
    permission is already part of that grant, so no separate consent flow).

    Client secrets come from the same gcp-oauth.keys.json the Gmail MCP
    server uses (~/.gmail-mcp/, overridable via GMAIL_OAUTH_PATH).

    The Google libraries are imported lazily so the import of this module
    stays cheap and dependency-free.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = WORKSPACE_DIR / "token.json"
    oauth_path = Path(
        os.environ.get(
            "GMAIL_OAUTH_PATH",
            str(Path.home() / ".gmail-mcp" / "gcp-oauth.keys.json"),
        )
    )

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (ValueError, KeyError, OSError):
            creds = None  # corrupt/unreadable cache — fall back to fresh consent

    if not creds or not creds.valid:
        needs_consent = True
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                needs_consent = False
            except Exception:  # noqa: BLE001 — revoked/expired grant
                creds = None
        if needs_consent:
            if not oauth_path.exists():
                raise RuntimeError(
                    f"OAuth client secrets not found at {oauth_path} — set "
                    "GMAIL_OAUTH_PATH or place gcp-oauth.keys.json in "
                    "~/.gmail-mcp/."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(oauth_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# --------------------------------------------------------------------------- #
# Meeting-request extraction (Gemini)
# --------------------------------------------------------------------------- #

# Dedicated model for structured extraction — deliberately NOT the llm_client
# chain: that one answers in the assistant's voice, while this call must
# return machine-readable JSON only. gemini-2.5-flash is the spec default;
# override with CALENDAR_GEMINI_MODEL in .env if Google deprecates it for
# your account (it 404s for new users as of Sep 2026 — see .env).
_MEETING_MODEL = os.environ.get("CALENDAR_GEMINI_MODEL", "gemini-2.5-flash")

# Output budget for the extraction call. On reasoning models (gemini-3.x)
# max_output_tokens covers INVISIBLE thinking tokens too, so the cap must be
# generous even for this ~120-token JSON answer: with the old 2048 cap, a
# heavy thinking draw (~1900 tokens) left the answer cut mid-string — the
# "Unterminated string starting at: line 1 column 132 (char 131)" bug.
_MAX_OUTPUT_TOKENS = 8192
# One retry at double the budget when the first answer is truncated anyway;
# the truncation is reported as a parsing_error only if the retry truncates.
_RETRY_OUTPUT_TOKENS = 16384

# Lazily-resolved feature flag: does the installed google.generativeai SDK's
# GenerationConfig accept a thinking_config field (needed to send
# thinking_budget=0)? SDK 0.8.6 rejects unknown fields with a ValueError, so
# the flag stays False there and only the generous budget protects the call.
_THINKING_DISABLED_SUPPORTED: bool | None = None


_MEETING_SYSTEM_PROMPT = """\
You extract meeting details from email threads.

Respond with ONLY a valid JSON object — no markdown, no code fences, no
commentary before or after it. Schema:

{
  "proposed_times": ["2026-09-10T14:00:00+02:00"],
  "attendees": ["sam.chen@vendorly.io"],
  "topic": "Vendorly rollout kickoff",
  "duration_minutes": 45
}

Rules:
- Resolve relative day names ("tomorrow", "Thursday", "next week") against
  today's date given in the user prompt.
- proposed_times: ISO-8601 datetime strings with UTC offset, in
  chronological order. Only times actually proposed in the thread — never
  invent times. Empty list when no concrete time is proposed.
- attendees: bare email addresses only; strip display names from headers
  like "Sam Chen <sam.chen@vendorly.io>".
- duration_minutes: integer; when the thread does not state one, use 30.
- topic: one short line, no trailing period.
"""


def _strip_code_fences(text: str) -> str:
    """Remove ``` / ```json fences Gemini sometimes wraps its JSON in."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _meeting_prompt(thread: dict) -> str:
    """Build the user prompt: today's date + the full thread transcript."""
    today = datetime.now().astimezone()
    parts = []
    for i, msg in enumerate(thread.get("messages") or [], 1):
        parts.append(
            f"--- Message {i} ---\n"
            f"From: {msg.get('from', '(unknown sender)')}\n"
            f"Date: {msg.get('date', '')}\n"
            f"{(msg.get('body') or '').strip()}"
        )
    transcript = "\n\n".join(parts) if parts else "(no messages)"
    return (
        f"Today is {today.strftime('%A, %d %B %Y')} "
        f"(local timezone {today.strftime('%z')}).\n\n"
        f"Email thread — subject: {thread.get('subject') or '(no subject)'}\n\n"
        f"{transcript}\n\n"
        "Extract the meeting details as specified."
    )


def _finish_reason(response) -> str:
    """
    Why the first candidate stopped — 'STOP', 'MAX_TOKENS', 'SAFETY', ... —
    '' when the response carries no usable candidate. Some SDK versions
    return an enum (with .name), some a bare int; both are handled.
    """
    try:
        candidate = response.candidates[0]
    except (AttributeError, IndexError, TypeError):
        return ""
    reason = getattr(candidate, "finish_reason", None)
    if reason is None:
        return ""
    name = getattr(reason, "name", None)
    if name:
        return str(name)
    try:
        return {1: "STOP", 2: "MAX_TOKENS"}.get(int(reason), str(reason))
    except (TypeError, ValueError):
        return str(reason)


def _meeting_generation_config(max_tokens: int, disable_thinking: bool = True) -> dict:
    """
    Generation config for the extraction call.

    max_output_tokens is the TOTAL budget (thinking + visible answer) on
    reasoning models. disable_thinking adds thinking_budget=0 only when the
    installed SDK's GenerationConfig accepts a thinking_config field; SDK
    0.8.6 validates fields strictly and rejects unknown keys with ValueError
    ("Unknown field for GenerationConfig"), so there the flag is False and
    the generous max_output_tokens budget alone absorbs the thinking draw.
    If the SDK ever gains the field, thinking is disabled automatically
    (removing the nondeterministic budget pressure and most of the latency)
    with no further code change.
    """
    config: dict = {"temperature": 0, "max_output_tokens": max_tokens}
    if disable_thinking and _THINKING_DISABLED_SUPPORTED:
        config["thinking_config"] = {"thinking_budget": 0}
    return config


def parse_meeting_request(thread: dict) -> dict:
    """
    Extract meeting details from an email thread with Gemini (gemini-2.5-flash).

    `thread` is a pipeline-format dict: {"id", "subject",
    "messages": [{"from", "date", "body"}, ...]}. All messages are
    concatenated into one transcript, and today's date is baked into the
    prompt so relative day names ("Thursday", "tomorrow") resolve correctly.

    Returns on success:
        {"proposed_times": [...],   # ISO-8601 datetime strings
         "attendees": [...],        # bare email addresses
         "topic": "...",            # one-line summary
         "duration_minutes": 30}    # int; 30 when the model omits it

    Returns on any failure — missing GEMINI_API_KEY, API error, blocked or
    empty response, unparseable JSON, wrong field types (nothing ever
    raises):
        {"parsing_error": "...", "raw": "..."}
    """
    # Use the shared backend chain so calendar parsing follows the same
    # UnoRouter -> OpenRouter -> Gemini configuration as triage and drafting.
    # The old direct Gemini implementation remains below as legacy reference,
    # but deployments should never require GEMINI_API_KEY just to book.
    raw = ""
    try:
        from llm_client import generate_text

        raw = generate_text(
            system_prompt=_MEETING_SYSTEM_PROMPT,
            user_prompt=_meeting_prompt(thread),
            max_tokens=1024,
        )
        data = json.loads(_strip_code_fences(raw))
        if not isinstance(data, dict):
            raise ValueError("LLM did not return a JSON object")

        proposed_times = data.get("proposed_times")
        if not isinstance(proposed_times, list):
            raise ValueError("proposed_times must be a list of datetime strings")
        attendees = data.get("attendees")
        if not isinstance(attendees, list):
            raise ValueError("attendees must be a list of email addresses")
        topic = str(data.get("topic", "")).strip()
        if not topic:
            raise ValueError("topic is missing or empty")
        try:
            duration_minutes = int(data.get("duration_minutes", 30))
        except (TypeError, ValueError) as exc:
            raise ValueError("duration_minutes must be an integer") from exc

        return {
            "proposed_times": [str(t).strip() for t in proposed_times],
            "attendees": [str(a).strip() for a in attendees],
            "topic": topic,
            "duration_minutes": duration_minutes,
        }
    except Exception as exc:  # noqa: BLE001 - parser failures are shown in UI
        return {"parsing_error": str(exc), "raw": raw}

    # Legacy direct-Gemini implementation retained below for reference.
    raw = ""
    try:
        global _THINKING_DISABLED_SUPPORTED

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")

        import warnings

        with warnings.catch_warnings():
            # The package raises a FutureWarning on import; silenced locally
            # (same trick llm_client.py uses).
            warnings.simplefilter("ignore", FutureWarning)
            import google.generativeai as genai

        # Resolve the thinking_config feature flag once per process (see
        # _meeting_generation_config for why it must be detected, not assumed).
        if _THINKING_DISABLED_SUPPORTED is None:
            _THINKING_DISABLED_SUPPORTED = (
                "thinking_config"
                in getattr(genai.types.GenerationConfig, "__dataclass_fields__", {})
            )

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name=_MEETING_MODEL,
            system_instruction=_MEETING_SYSTEM_PROMPT,
        )

        def _generate(max_tokens: int, disable_thinking: bool = True):
            """One extraction call at the given output budget."""
            return model.generate_content(
                _meeting_prompt(thread),
                generation_config=_meeting_generation_config(
                    max_tokens, disable_thinking
                ),
            )

        # Generous budget up front: thinking and the visible answer share
        # max_output_tokens (the old 2048 cap let a heavy thinking draw cut
        # the JSON mid-string — see _MAX_OUTPUT_TOKENS above).
        try:
            response = _generate(_MAX_OUTPUT_TOKENS)
        except Exception as exc:  # noqa: BLE001 — only a thinking_config
            # rejection (SDK field validation) falls through to the plain
            # config; every other failure re-raises into the normal path.
            if "thinking" not in str(exc).lower():
                raise
            response = _generate(_MAX_OUTPUT_TOKENS, disable_thinking=False)

        # MAX_TOKENS means thinking + answer together crossed the budget and
        # the JSON was cut mid-string. One retry at double the budget; the
        # truncation is only reported as a parsing_error if the retry
        # truncates too.
        if _finish_reason(response) == "MAX_TOKENS":
            response = _generate(_RETRY_OUTPUT_TOKENS)
            if _finish_reason(response) == "MAX_TOKENS":
                raise RuntimeError(
                    "Gemini response truncated by max_output_tokens twice "
                    f"({_MAX_OUTPUT_TOKENS} then {_RETRY_OUTPUT_TOKENS}) — "
                    "thinking consumed the budget before the JSON closed"
                )

        try:
            raw = (response.text or "").strip()
        except (ValueError, AttributeError, IndexError):
            raw = ""
        if not raw:
            raise RuntimeError(
                "Gemini returned an empty response (blocked, truncated, or "
                "thinking-only?)"
            )

        data = json.loads(_strip_code_fences(raw))
        if not isinstance(data, dict):
            raise ValueError("Gemini did not return a JSON object")

        proposed_times = data.get("proposed_times")
        if not isinstance(proposed_times, list):
            raise ValueError("proposed_times must be a list of datetime strings")
        attendees = data.get("attendees")
        if not isinstance(attendees, list):
            raise ValueError("attendees must be a list of email addresses")
        topic = str(data.get("topic", "")).strip()
        if not topic:
            raise ValueError("topic is missing or empty")
        try:
            duration_minutes = int(data.get("duration_minutes", 30))
        except (TypeError, ValueError):
            raise ValueError(
                f"duration_minutes is not an integer: "
                f"{data.get('duration_minutes')!r}"
            )

        return {
            "proposed_times": [str(t).strip() for t in proposed_times],
            "attendees": [str(a).strip() for a in attendees],
            "topic": topic,
            "duration_minutes": duration_minutes,
        }
    except Exception as exc:  # noqa: BLE001 — every failure becomes parsing_error
        return {"parsing_error": str(exc), "raw": raw}


# --------------------------------------------------------------------------- #
# Availability (FreeBusy on the primary calendar)
# --------------------------------------------------------------------------- #

# Why the last availability answer was "busy"/None — "" after a healthy check.
# check_availability() fails closed (every failure counts as busy), so this
# companion text is what keeps a broken check — auth errors, quota, or the
# Calendar API being disabled on the OAuth project (HttpError 403
# accessNotConfigured) — distinguishable from a genuinely full calendar.
_LAST_CHECK_ERROR = ""


def last_availability_error() -> str:
    """
    Why the most recent check_availability()/find_free_slot() call said "busy"
    or returned None — "" when the check itself was healthy.

    check_availability() fails closed (any failure counts as busy), so this
    companion text is how callers tell a broken calendar check apart from a
    genuinely full calendar; find_free_slot() sets it when nothing bookable
    remains (all proposed times in the past, no usable times, bad duration).
    """
    return _LAST_CHECK_ERROR


def _ensure_utc_suffix(value: str) -> str:
    """Append 'Z' to an ISO-8601 timestamp that carries no timezone info.

    Timestamps already ending in "Z" or an explicit UTC offset ("+02:00",
    "+0530", ...) are returned untouched; naive ones get "Z" so the
    FreeBusy API always receives timezone-qualified times.
    """
    value = str(value).strip()
    if value.endswith(("Z", "z")) or re.search(r"[+-]\d{2}:?\d{2}$", value):
        return value
    return value + "Z"


def check_availability(time_min: str, time_max: str) -> bool:
    """
    True when the user's primary calendar is free in [time_min, time_max).

    Both bounds are ISO-8601 strings; a "Z" suffix is appended to either one
    that lacks timezone info. The FreeBusy API is queried for the "primary"
    calendar and True means no busy intervals overlap the window.

    Safe default: any failure — auth/service errors, API errors, missing or
    malformed responses — returns False (busy), so callers never need a
    try/except and never double-book on a broken check. The failure text lands
    in last_availability_error() so a broken check (e.g. the Calendar API not
    enabled on the OAuth project — HttpError 403 accessNotConfigured) stays
    distinguishable from a genuinely busy calendar, where the text stays "".
    """
    global _LAST_CHECK_ERROR
    try:
        service = _build_calendar_service()
        body = {
            "timeMin": _ensure_utc_suffix(time_min),
            "timeMax": _ensure_utc_suffix(time_max),
            "items": [{"id": "primary"}],
        }
        result = service.freebusy().query(body=body).execute()
        entry = (result.get("calendars") or {}).get("primary")
        if not entry:
            _LAST_CHECK_ERROR = (
                "FreeBusy response has no entry for the 'primary' calendar"
            )
            return False  # primary missing from the response — safe default
        if entry.get("errors"):
            _LAST_CHECK_ERROR = (
                "FreeBusy reported errors for 'primary': "
                + json.dumps(entry["errors"])
            )
            return False
        _LAST_CHECK_ERROR = ""  # healthy check — busy here means genuinely busy
        return not entry.get("busy", [])
    except Exception as exc:  # noqa: BLE001 — safe default: "busy" on any failure
        _LAST_CHECK_ERROR = f"{type(exc).__name__}: {exc}"
        return False


def find_free_slot(proposed_times: list, duration_minutes) -> str | None:
    """
    Return the first proposed time whose slot is free on the primary
    calendar, or None when every slot is busy, in the past, malformed, or the
    inputs are unusable.

    `proposed_times` is a list of ISO-8601 strings (e.g. from
    parse_meeting_request()["proposed_times"]); each slot's end is
    start + duration_minutes. Malformed time strings, blanks, and
    non-string entries are skipped gracefully; a non-integer or
    non-positive duration yields None.

    Slots that already ended before "now" are skipped too — a meeting can
    never be booked in the past (this is what keeps a stale thread like
    "Thursday 2pm?" from booking yesterday). A bare time with no UTC offset
    is read as LOCAL time — the natural reading of "14:00" in an email — and
    the slot is returned with that explicit offset, so FreeBusy checks and
    create_event books the very same instant.

    When nothing is bookable the reason lands in last_availability_error()
    ("all N proposed time(s) are already in the past", "no usable proposed
    times", a bad duration, or the last check_availability() failure).
    """
    global _LAST_CHECK_ERROR
    try:
        minutes = int(duration_minutes)
    except (TypeError, ValueError):
        _LAST_CHECK_ERROR = f"duration_minutes is not an integer: {duration_minutes!r}"
        return None
    if minutes <= 0:
        _LAST_CHECK_ERROR = f"duration_minutes must be positive, got {minutes}"
        return None

    now = datetime.now().astimezone()
    checked = 0  # reached check_availability() — its text is authoritative
    in_past = 0
    for slot in proposed_times or []:
        if not isinstance(slot, str):
            continue
        text = slot.strip()
        if not text:
            continue
        try:
            # fromisoformat() only understands a "Z" suffix from Python
            # 3.11 on — normalise it for older interpreters.
            start = datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            )
        except (ValueError, TypeError):
            continue  # malformed time string — skip gracefully
        if start.tzinfo is None:
            start = start.replace(tzinfo=now.tzinfo)
            text = start.isoformat()
        end = start + timedelta(minutes=minutes)
        if end <= now:
            in_past += 1
            continue  # a slot that is already over can never be booked
        checked += 1
        if check_availability(start.isoformat(), end.isoformat()):
            return text

    if not checked:
        if in_past:
            _LAST_CHECK_ERROR = (
                f"all {in_past} proposed time(s) are already in the past — "
                "ask for new times"
            )
        else:
            _LAST_CHECK_ERROR = (
                "no usable proposed times (list empty, blank, or malformed)"
            )
    # When checked > 0 the last check_availability() call already left the
    # right text behind: "" for a healthy all-busy answer, its failure text
    # when the calendar check itself is broken.
    return None


# --------------------------------------------------------------------------- #
# Event creation (events().insert on the primary calendar)
# --------------------------------------------------------------------------- #


def create_event(
    summary: str,
    start_time: str,
    duration_minutes,
    attendees: list,
    description: str = "",
) -> dict:
    """
    Create a Google Calendar event on the primary calendar and return the
    created event dict from the API — the full response, including "id",
    "htmlLink", "summary", "start" and "end".

    `start_time` is an ISO-8601 string (as produced by
    parse_meeting_request()["proposed_times"] / find_free_slot()); the end
    is computed as start + duration_minutes. A trailing "Z" is normalised
    before fromisoformat() (Python < 3.11 — same trick as find_free_slot()).
    Both start and end are sent with timeZone "UTC".

    `attendees` is a list of email addresses; only entries that contain "@"
    are included, so junk or display-name-only entries never reach the API.
    When nobody valid remains, the "attendees" key is omitted entirely.

    sendUpdates="all" makes Google email an invitation to every attendee.
    Auth/network/API errors propagate to the caller (same convention as
    engine.send_reply() — this is a write, so failures must be visible).
    """
    text = str(start_time).strip()
    start = datetime.fromisoformat(
        text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    )
    end = start + timedelta(minutes=int(duration_minutes))

    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
    }
    valid_attendees = [
        {"email": str(attendee).strip()}
        for attendee in (attendees or [])
        if attendee and "@" in str(attendee)
    ]
    if valid_attendees:
        body["attendees"] = valid_attendees

    service = _build_calendar_service()
    return (
        service.events()
        .insert(calendarId="primary", body=body, sendUpdates="all")
        .execute()
    )
