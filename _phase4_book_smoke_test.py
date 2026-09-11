"""Smoke test for the Approval Gate Book Meeting capability (app.py +
calendar_engine.py).

Runs the app under Streamlit's AppTest harness with the calendar_engine
trio stubbed, so nothing touches the network, no real event is created and
no invitation email is sent:

  1. Book happy path — a meeting-request thread's approved record shows
     [Send] [Book Meeting] side by side; clicking Book Meeting runs the
     full parse-check-book flow (stubbed), stores the created event in
     session state booked, and the calendar link replaces the button while
     Send stays.
  2. Already booked — no Book button; the calendar link shows instead.
  3. Parse failure — parsing_error surfaces as an error; nothing booked,
     button kept for a retry.
  4. No free slot — find_free_slot() returning None surfaces a warning;
     nothing booked, button kept for a retry.
  5. Non-meeting thread — Book Meeting is still offered (secondary button);
     clicking it books from whatever the parse produces.

Run:  .venv\\Scripts\\python.exe _phase4_book_smoke_test.py
"""

from __future__ import annotations

import sys

from streamlit.testing.v1 import AppTest

import calendar_engine  # same process as AppTest -> monkeypatching works
import task_logger  # Book Meeting now audits to action_log.json

# Mirror the other smoke tests: Windows consoles default to a legacy codepage
# while assert text can contain arbitrary Unicode.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# --- Stub plumbing ---------------------------------------------------------- #
# _get_calendar_engine() is @st.cache_resource and does `from calendar_engine
# import ...` on its first call, so the stubs must be in place BEFORE the first
# AppTest run. The dispatcher indirection lets each test swap behaviour even
# though the cache holds one resolved callable for the whole process.

_PARSE_CALLS: list[dict] = []
_FIND_CALLS: list[dict] = []
_CREATE_CALLS: list[dict] = []

_HAPPY_PARSE = {
    "proposed_times": ["2026-09-10T14:00:00+02:00", "2026-09-11T09:00:00+02:00"],
    "attendees": ["sam.chen@vendorly.io"],
    "topic": "Vendorly rollout kickoff",
    "duration_minutes": 45,
}

_HAPPY_EVENT = {
    "id": "stub-event-001",
    "htmlLink": "https://calendar.google.com/calendar/event?eid=stub",
    "summary": "Vendorly rollout kickoff",
    "start": {"dateTime": "2026-09-10T14:00:00+02:00", "timeZone": "UTC"},
    "end": {"dateTime": "2026-09-10T14:45:00+02:00", "timeZone": "UTC"},
}


def _happy_parse(thread):
    _PARSE_CALLS.append({"thread": thread})
    return dict(_HAPPY_PARSE)


def _boom_parse(thread):
    return {"parsing_error": "stub: Gemini said no", "raw": "raw text"}


def _happy_find(proposed_times, duration_minutes):
    _FIND_CALLS.append(
        {"proposed_times": proposed_times, "duration_minutes": duration_minutes}
    )
    return proposed_times[0] if proposed_times else None


def _no_slot_find(proposed_times, duration_minutes):
    _FIND_CALLS.append(
        {"proposed_times": proposed_times, "duration_minutes": duration_minutes}
    )
    return None


def _happy_create(**kwargs):
    _CREATE_CALLS.append(kwargs)
    return dict(_HAPPY_EVENT)


_CURRENT = {
    "parse": _happy_parse,
    "find": _happy_find,
    "create": _happy_create,
}

calendar_engine.parse_meeting_request = lambda thread: _CURRENT["parse"](thread)  # type: ignore[assignment]
calendar_engine.find_free_slot = (  # type: ignore[assignment]
    lambda proposed_times, duration_minutes: _CURRENT["find"](
        proposed_times, duration_minutes
    )
)
calendar_engine.create_event = lambda **kwargs: _CURRENT["create"](**kwargs)  # type: ignore[assignment]

# --- Fixtures --------------------------------------------------------------- #

THREAD_A = {
    "id": "sample-0",
    "subject": "Kickoff call — when works for you?",
    "messages": [
        {
            "from": "Sam Chen <sam.chen@vendorly.io>",
            "date": "Tue, 8 Sep 2026",
            "body": "Can we do a kickoff this week? Thursday 2pm or Friday 9am, "
            "45 minutes.",
        },
    ],
}

ITEM_A = {
    "id": "sample-0",
    "sender": "Sam Chen <sam.chen@vendorly.io>",
    "subject": "Kickoff call — when works for you?",
    "snippet": "Can we do a kickoff this week? Thursday 2pm or Friday 9am.",
    "priority": "needs-reply",
    "category": "meeting-request",
    "action": "draft-reply",
}

DRAFT_REC = {
    "draft": "Thursday 2pm works great — sending an invite now.",
    "model": "stub-model",
    "subject": ITEM_A["subject"],
    "reply_to": ITEM_A["sender"],
}

APPROVED_REC = {
    "approved_at": "2026-09-09T10:00:00+02:00",
    "status": "ready_to_send",
    "edited": False,
    "id": "sample-0",
    "thread_subject": ITEM_A["subject"],
    "reply_to": ITEM_A["sender"],
    "model": "stub-model",
    "draft": DRAFT_REC["draft"],
    "priority": "needs-reply",
}


def _gate_with_approved(category: str = "meeting-request") -> AppTest:
    """Boot the app and seed one approved thread at the Approval Gate."""
    at = AppTest.from_file("app.py", default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    at.session_state["threads"] = [THREAD_A]
    at.session_state["triaged"] = [dict(ITEM_A, category=category)]
    at.session_state["drafts"] = {"sample-0": dict(DRAFT_REC)}
    at.session_state["approved"] = {"sample-0": dict(APPROVED_REC)}
    at.button(key="nav_2").click().run()
    assert not at.exception, at.exception
    return at


# --- Tests ------------------------------------------------------------------ #


def test_book_happy_path() -> None:
    _PARSE_CALLS.clear()
    _FIND_CALLS.clear()
    _CREATE_CALLS.clear()
    _CURRENT.update(parse=_happy_parse, find=_happy_find, create=_happy_create)
    # Fresh audit trail so the log assertions below are deterministic.
    if task_logger.LOG_PATH.exists():
        task_logger.LOG_PATH.unlink()
    at = _gate_with_approved()

    # Side-by-side action row: Send + Book Meeting on the approved record.
    assert [b for b in at.button if b.key == "send_sample-0"], [b.key for b in at.button]
    assert [b for b in at.button if b.key == "book_sample-0"], [b.key for b in at.button]

    at.button(key="book_sample-0").click().run()
    assert not at.exception, at.exception

    # The stub trio ran: parse(thread) -> find(proposed, duration) -> create(...).
    assert len(_PARSE_CALLS) == 1, _PARSE_CALLS
    assert _PARSE_CALLS[0]["thread"]["id"] == "sample-0", _PARSE_CALLS
    assert len(_FIND_CALLS) == 1, _FIND_CALLS
    assert _FIND_CALLS[0]["proposed_times"] == _HAPPY_PARSE["proposed_times"], _FIND_CALLS
    assert _FIND_CALLS[0]["duration_minutes"] == 45, _FIND_CALLS
    assert len(_CREATE_CALLS) == 1, _CREATE_CALLS
    call = _CREATE_CALLS[0]
    assert call["summary"] == "Vendorly rollout kickoff", call
    assert call["start_time"] == "2026-09-10T14:00:00+02:00", call
    assert call["duration_minutes"] == 45, call
    assert call["attendees"] == ["sam.chen@vendorly.io"], call

    # Session state: the created event dict is stored under the thread id.
    booked = at.session_state["booked"]["sample-0"]
    assert booked["id"] == "stub-event-001", booked
    assert booked["htmlLink"].startswith("https://calendar.google.com/"), booked
    assert booked["start"]["timeZone"] == "UTC", booked

    # Audit trail: the successful booking appended one "booked" record.
    log = task_logger.get_action_log()
    assert len(log) == 1, log
    entry = log[0]
    assert entry["action_type"] == "booked", entry
    assert entry["thread_subject"] == ITEM_A["subject"], entry
    assert entry["detail"] == "Vendorly rollout kickoff", entry  # parsed topic
    assert entry["id"] == "stub-event-001", entry
    assert entry["timestamp"], entry

    # UI: the Book button is replaced by the calendar link; Send remains.
    assert not [b for b in at.button if b.key == "book_sample-0"], "Book button must disappear"
    assert [b for b in at.button if b.key == "send_sample-0"], "Send button must remain"
    assert any("Open in Google Calendar" in s.value for s in at.success), [
        s.value for s in at.success
    ]
    print("PASS 1/5 Book happy path: parse->check->book ran, event stored, link replaces button")


def test_already_booked_shows_link() -> None:
    at = _gate_with_approved()
    at.session_state["booked"] = {"sample-0": dict(_HAPPY_EVENT)}
    at.run()
    assert not at.exception, at.exception
    assert not [b for b in at.button if b.key == "book_sample-0"], "Book button must be gone"
    assert [b for b in at.button if b.key == "send_sample-0"], "Send button must remain"
    assert any("Open in Google Calendar" in s.value for s in at.success), [
        s.value for s in at.success
    ]
    print("PASS 2/5 Already booked: calendar link shown instead of the button")


def test_parse_failure() -> None:
    _PARSE_CALLS.clear()
    _CREATE_CALLS.clear()
    _CURRENT["parse"] = _boom_parse
    at = _gate_with_approved()

    at.button(key="book_sample-0").click().run()
    assert not at.exception, at.exception

    assert any("could not parse the meeting request" in e.value for e in at.error), [
        e.value for e in at.error
    ]
    assert at.session_state["booked"] == {}, at.session_state["booked"]
    assert not _CREATE_CALLS, "create_event must not run when parsing fails"
    assert [b for b in at.button if b.key == "book_sample-0"], "Book button must remain for retry"
    _CURRENT["parse"] = _happy_parse
    print("PASS 3/5 Parse failure: error surfaced, nothing booked, retry kept")


def test_no_free_slot() -> None:
    _PARSE_CALLS.clear()
    _FIND_CALLS.clear()
    _CREATE_CALLS.clear()
    _CURRENT["find"] = _no_slot_find
    at = _gate_with_approved()

    at.button(key="book_sample-0").click().run()
    assert not at.exception, at.exception

    assert any("None of the proposed times are free" in w.value for w in at.warning), [
        w.value for w in at.warning
    ]
    assert at.session_state["booked"] == {}, at.session_state["booked"]
    assert not _CREATE_CALLS, "create_event must not run when no slot is free"
    assert [b for b in at.button if b.key == "book_sample-0"], "Book button must remain for retry"
    _CURRENT["find"] = _happy_find
    print("PASS 4/5 No free slot: warning surfaced, nothing booked, retry kept")


def test_non_meeting_thread() -> None:
    _PARSE_CALLS.clear()
    _FIND_CALLS.clear()
    _CREATE_CALLS.clear()
    _CURRENT.update(parse=_happy_parse, find=_happy_find, create=_happy_create)
    # Fresh audit trail: this test books too, so it must log as well.
    if task_logger.LOG_PATH.exists():
        task_logger.LOG_PATH.unlink()
    at = _gate_with_approved(category="task-request")

    # Book Meeting is offered on non-meeting threads too — as a secondary
    # button so the primary action stays Send (asserted via its "you can
    # still try" help text). Clicking it still books.
    assert [b for b in at.button if b.key == "send_sample-0"], [b.key for b in at.button]
    book = [b for b in at.button if b.key == "book_sample-0"]
    assert book, "Book Meeting must be offered on non-meeting threads too"
    assert "Not triaged as a meeting request" in (book[0].help or ""), book[0].help

    book[0].click().run()
    assert not at.exception, at.exception
    booked = at.session_state["booked"]["sample-0"]
    assert booked["id"] == "stub-event-001", booked
    assert len(_CREATE_CALLS) == 1, _CREATE_CALLS
    assert not [b for b in at.button if b.key == "book_sample-0"], "Book button must disappear"
    # Audit trail: booking from a non-meeting thread logs the same way.
    log = task_logger.get_action_log()
    assert len(log) == 1 and log[0]["action_type"] == "booked", log
    assert log[0]["id"] == "stub-event-001", log
    print("PASS 5/5 Non-meeting thread: secondary Book button offered and works")


if __name__ == "__main__":
    try:
        test_book_happy_path()
        test_already_booked_shows_link()
        test_parse_failure()
        test_no_free_slot()
        test_non_meeting_thread()
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        # Remove the test's audit-trail artifact so real usage starts fresh.
        if task_logger.LOG_PATH.exists():
            task_logger.LOG_PATH.unlink()
    print()
    print("All Book Meeting smoke checks passed (no event was actually created).")
