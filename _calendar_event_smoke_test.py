"""Smoke test for calendar_engine.create_event (offline, no network).

Stubs _build_calendar_service with a fake googleapiclient service that
records every events().insert() call and returns a canned Google-style
response, so no real event is created and no invitation email is sent:

  1. Happy path — naive start, 45 min, mixed attendees: asserts end is
     start + duration, the body carries summary/description/start/end with
     timeZone "UTC", only "@"-bearing attendees survive (as {"email": ...}),
     and the call uses calendarId="primary" + sendUpdates="all" and returns
     the full API response dict (id, htmlLink, summary, start, end).
  2. "Z"-suffixed start — end computed on the timezone-aware datetime.
  3. Offset start — end = start + duration, offset preserved in dateTime.
  4. No valid attendees — the "attendees" key is omitted from the body and
     description defaults to "".

Run:  .venv\\Scripts\\python.exe _calendar_event_smoke_test.py
"""

from __future__ import annotations

import sys

import calendar_engine

# Mirror the other smoke tests: Windows consoles default to a legacy
# codepage while assert text can contain arbitrary Unicode.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# --- Stub plumbing ---------------------------------------------------------- #

_CALLS: list[dict] = []


class _FakeInsert:
    def __init__(self, body, calendarId, sendUpdates):
        _CALLS.append(
            {"body": body, "calendarId": calendarId, "sendUpdates": sendUpdates}
        )

    def execute(self):
        body = _CALLS[-1]["body"]
        return {
            "id": "stub-event-001",
            "htmlLink": "https://calendar.google.com/calendar/event?eid=stub",
            "summary": body["summary"],
            "description": body["description"],
            "start": body["start"],
            "end": body["end"],
            "attendees": body.get("attendees", []),
            "status": "confirmed",
        }


class _FakeEvents:
    def insert(self, *, body, calendarId, sendUpdates):
        return _FakeInsert(body, calendarId, sendUpdates)


class _FakeService:
    def events(self):
        return _FakeEvents()


calendar_engine._build_calendar_service = lambda: _FakeService()  # type: ignore[assignment]


# --- Tests ------------------------------------------------------------------ #


def test_happy_path() -> None:
    _CALLS.clear()
    created = calendar_engine.create_event(
        "Vendorly rollout kickoff",
        "2026-09-10T14:00:00",
        45,
        ["sam.chen@vendorly.io", "not-an-email", "", None],
        description="Kickoff for the Vendorly rollout.",
    )
    call = _CALLS[-1]
    assert call["calendarId"] == "primary", call
    assert call["sendUpdates"] == "all", call

    body = call["body"]
    assert body["summary"] == "Vendorly rollout kickoff", body
    assert body["description"] == "Kickoff for the Vendorly rollout.", body
    assert body["start"] == {"dateTime": "2026-09-10T14:00:00", "timeZone": "UTC"}, body["start"]
    assert body["end"] == {"dateTime": "2026-09-10T14:45:00", "timeZone": "UTC"}, body["end"]
    assert body["attendees"] == [{"email": "sam.chen@vendorly.io"}], body["attendees"]

    # Full API response dict comes straight back out.
    assert created["id"] == "stub-event-001", created
    assert created["htmlLink"].startswith("https://calendar.google.com/"), created
    assert created["summary"] == "Vendorly rollout kickoff", created
    assert created["start"]["timeZone"] == "UTC", created["start"]
    assert created["end"]["dateTime"] == "2026-09-10T14:45:00", created["end"]
    print("PASS 1/4 Happy path: end=start+45min, UTC, attendee filter, primary+sendUpdates=all")


def test_z_suffix() -> None:
    _CALLS.clear()
    calendar_engine.create_event("Zulu", "2026-09-10T14:00:00Z", 30, [])
    end = _CALLS[-1]["body"]["end"]["dateTime"]
    assert end == "2026-09-10T14:30:00+00:00", end
    print("PASS 2/4 'Z' suffix: parsed and end = start + 30min (+00:00)")


def test_offset_start() -> None:
    _CALLS.clear()
    calendar_engine.create_event("Offset", "2026-09-10T14:00:00+02:00", 15, [])
    body = _CALLS[-1]["body"]
    assert body["start"]["dateTime"] == "2026-09-10T14:00:00+02:00", body["start"]
    assert body["end"]["dateTime"] == "2026-09-10T14:15:00+02:00", body["end"]
    print("PASS 3/4 Offset start: end = start + 15min, offset preserved")


def test_no_valid_attendees() -> None:
    _CALLS.clear()
    calendar_engine.create_event("Solo", "2026-09-10T09:00:00", 30, ["nope", "", None])
    body = _CALLS[-1]["body"]
    assert "attendees" not in body, body
    assert body["description"] == "", body  # default description
    print("PASS 4/4 No valid attendees: key omitted; description defaults to ''")


if __name__ == "__main__":
    test_happy_path()
    test_z_suffix()
    test_offset_start()
    test_no_valid_attendees()
    print()
    print("All create_event smoke checks passed (no event was actually created).")