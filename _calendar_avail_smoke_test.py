"""Smoke test for calendar_engine availability (offline, no network).

Stubs _build_calendar_service with a fake freebusy service so
check_availability / find_free_slot never touch the network:

  1. Free window — True; last_availability_error() stays "" (healthy check).
  2. Busy window — False; text stays "" (a genuinely busy calendar is NOT a
     failure — the fail-closed default must not blur into every busy answer).
  3. FreeBusy raises — the disabled-API bug this guards against (HttpError
     403 accessNotConfigured): False AND the reason is captured, then a
     healthy call clears it again.
  4. FreeBusy entry carries "errors" — False + reason mentions them.
  5. find_free_slot returns the first FREE proposed time among busy ones.
  6. find_free_slot with only future busy times — None, reason "".
  7. find_free_slot with only past times — None + "already in the past"
     (a meeting can never be booked in the past).
  8. find_free_slot with an empty list — None + "no usable proposed times".
  9. Naive proposed time — read as LOCAL time; the returned slot carries the
     local offset so FreeBusy checks and create_event books the same instant.

Run:  .venv\\Scripts\\python.exe _calendar_avail_smoke_test.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import calendar_engine

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# --- Stub plumbing ---------------------------------------------------------- #

_RESPONDER = None  # callable(body) -> FreeBusy response dict


class _FakeQuery:
    def __init__(self, body):
        self.body = body

    def execute(self):
        return _RESPONDER(self.body)


class _FakeFreeBusy:
    def query(self, *, body):
        return _FakeQuery(body)


class _FakeService:
    def freebusy(self):
        return _FakeFreeBusy()


calendar_engine._build_calendar_service = lambda: _FakeService()  # type: ignore[assignment]


def _free(body):
    return {"calendars": {"primary": {"busy": []}}}


def _busy_all(body):
    return {
        "calendars": {
            "primary": {"busy": [{"start": body["timeMin"], "end": body["timeMax"]}]}
        }
    }


def _busy_before(cutoff: datetime):
    """Busy for windows starting at/before cutoff, free afterwards."""

    def responder(body):
        start = datetime.fromisoformat(body["timeMin"])
        if start <= cutoff:
            return _busy_all(body)
        return _free(body)

    return responder


def _disabled_api(body):
    raise RuntimeError(
        'HttpError 403 when requesting https://www.googleapis.com/calendar/v3/'
        "freeBusy returned \"Google Calendar API has not been used in project "
        '391072628386 before or it is disabled" Details: accessNotConfigured'
    )


def _errors_entry(body):
    return {
        "calendars": {
            "primary": {
                "busy": [],
                "errors": [{"domain": "usageLimits", "reason": "accessNotConfigured"}],
            }
        }
    }


def _missing_primary(body):
    return {"calendars": {}}


# Fixed reference points, computed from *now* so the test never goes stale.
NOW = datetime.now().astimezone()
FUTURE_A = NOW + timedelta(days=2, hours=3)
FUTURE_B = NOW + timedelta(days=3, hours=4)
PAST = NOW - timedelta(days=1)


# --- Tests ------------------------------------------------------------------ #


def test_free_window() -> None:
    global _RESPONDER
    _RESPONDER = _free
    assert calendar_engine.check_availability(
        FUTURE_A.isoformat(), (FUTURE_A + timedelta(hours=1)).isoformat()
    )
    assert calendar_engine.last_availability_error() == "", (
        calendar_engine.last_availability_error()
    )
    print("PASS 1/9 Free window: True, error text cleared")


def test_busy_window_is_not_a_failure() -> None:
    global _RESPONDER
    _RESPONDER = _busy_all
    assert not calendar_engine.check_availability(
        FUTURE_A.isoformat(), (FUTURE_A + timedelta(hours=1)).isoformat()
    )
    assert calendar_engine.last_availability_error() == "", (
        calendar_engine.last_availability_error()
    )
    print("PASS 2/9 Busy window: False but healthy — text stays empty")


def test_raising_freebusy_captures_reason() -> None:
    global _RESPONDER
    _RESPONDER = _disabled_api
    assert not calendar_engine.check_availability(
        FUTURE_A.isoformat(), (FUTURE_A + timedelta(hours=1)).isoformat()
    )
    reason = calendar_engine.last_availability_error()
    assert "403" in reason and "disabled" in reason, reason
    # A healthy call clears the reason again — no stale text survives.
    _RESPONDER = _free
    assert calendar_engine.check_availability(
        FUTURE_B.isoformat(), (FUTURE_B + timedelta(hours=1)).isoformat()
    )
    assert calendar_engine.last_availability_error() == "", (
        calendar_engine.last_availability_error()
    )
    print("PASS 3/9 Raising FreeBusy: reason captured, cleared on next healthy call")


def test_errors_entry_captured() -> None:
    global _RESPONDER
    _RESPONDER = _errors_entry
    assert not calendar_engine.check_availability(
        FUTURE_A.isoformat(), (FUTURE_A + timedelta(hours=1)).isoformat()
    )
    assert "accessNotConfigured" in calendar_engine.last_availability_error(), (
        calendar_engine.last_availability_error()
    )
    _RESPONDER = _missing_primary
    assert not calendar_engine.check_availability(
        FUTURE_A.isoformat(), (FUTURE_A + timedelta(hours=1)).isoformat()
    )
    assert "no entry" in calendar_engine.last_availability_error(), (
        calendar_engine.last_availability_error()
    )
    print("PASS 4/9 errors[] entry and missing primary: both captured as reasons")


def test_first_free_slot_wins() -> None:
    global _RESPONDER
    _RESPONDER = _busy_before(FUTURE_A)
    slot = calendar_engine.find_free_slot(
        [FUTURE_A.isoformat(), FUTURE_B.isoformat()], 45
    )
    assert slot == FUTURE_B.isoformat(), slot
    assert calendar_engine.last_availability_error() == "", (
        calendar_engine.last_availability_error()
    )
    print("PASS 5/9 find_free_slot: first FREE proposed time wins")


def test_all_future_busy() -> None:
    global _RESPONDER
    _RESPONDER = _busy_all
    slot = calendar_engine.find_free_slot(
        [FUTURE_A.isoformat(), FUTURE_B.isoformat()], 30
    )
    assert slot is None, slot
    assert calendar_engine.last_availability_error() == "", (
        calendar_engine.last_availability_error()
    )
    print("PASS 6/9 All future slots busy: None, healthy empty reason")


def test_all_past_skipped() -> None:
    global _RESPONDER
    _RESPONDER = _free  # even though the calendar is free, the past is untouchable
    slot = calendar_engine.find_free_slot([PAST.isoformat()], 60)
    assert slot is None, slot
    assert "already in the past" in calendar_engine.last_availability_error(), (
        calendar_engine.last_availability_error()
    )
    print("PASS 7/9 All proposed times in the past: None + past reason")


def test_empty_proposed_times() -> None:
    global _RESPONDER
    _RESPONDER = _free
    slot = calendar_engine.find_free_slot([], 60)
    assert slot is None, slot
    assert "no usable proposed times" in calendar_engine.last_availability_error(), (
        calendar_engine.last_availability_error()
    )
    print("PASS 8/9 Empty proposed list: None + usable-times reason")


def test_naive_time_read_as_local() -> None:
    global _RESPONDER
    _RESPONDER = _free
    naive = (NOW + timedelta(days=2)).replace(tzinfo=None).isoformat()
    slot = calendar_engine.find_free_slot([naive], 30)
    assert slot is not None, slot
    parsed = datetime.fromisoformat(slot)
    assert parsed.tzinfo is not None, slot
    assert parsed.utcoffset() == NOW.utcoffset(), (slot, NOW.utcoffset())
    assert calendar_engine.last_availability_error() == "", (
        calendar_engine.last_availability_error()
    )
    print(f"PASS 9/9 Naive time: read as local — slot returned as {slot}")


if __name__ == "__main__":
    test_free_window()
    test_busy_window_is_not_a_failure()
    test_raising_freebusy_captures_reason()
    test_errors_entry_captured()
    test_first_free_slot_wins()
    test_all_future_busy()
    test_all_past_skipped()
    test_empty_proposed_times()
    test_naive_time_read_as_local()
    print()
    print("All availability smoke checks passed (no network, nothing booked).")

