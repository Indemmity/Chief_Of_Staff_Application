"""Read-only diagnostic for the Book Meeting "None of the proposed times are
free (or none were usable)" warning.

Replays the exact steps app.py's _book_meeting_flow() runs — without creating
any event — and prints everything the UI hides:

  1. Current local time.
  2. parse_meeting_request() on the design-review sample thread (one real
     Gemini call, the same one the app makes when Book Meeting is clicked).
  3. The primary calendar's id + timeZone.
  4. A DIRECT FreeBusy query per parsed proposed time, with the raw response
     (busy intervals AND any "errors" entry) — unlike check_availability(),
     which swallows every failure into a bare False.
  5. find_free_slot() replayed on the parsed times.
  6. A broad FreeBusy window (now -> +7 days) for context.

Run:  .venv\\Scripts\\python.exe _calendar_diag.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import calendar_engine

now = datetime.now().astimezone()
print(f"[1] Now: {now.isoformat()}  (today is {now.strftime('%A')})")

# --- The design-review sample thread (the meeting-request sample) ----------- #
thread = {
    "id": "19b7f1b3d5c62a08",
    "subject": "Design review: new onboarding flow — Thu Sep 10?",
    "messages": [
        {
            "from": "Hana Kobayashi <hana.kobayashi@northwind-labs.com>",
            "date": "Mon, 7 Sep 2026 09:45:30 +0000",
            "body": (
                "The clickable prototype for the new onboarding flow is ready in "
                "Figma (link in the design channel).\n\n"
                "I'd like to run the design review this week so engineering has "
                "enough runway before the Sep 18 feature freeze. Two slots on the "
                "table:\n\n"
                "- Thursday, Sep 10, 14:00-15:00\n"
                "- Friday, Sep 11, 11:00-12:00\n\n"
                "I specifically need you there since the review touches the API "
                "surface and the auth handoff. Agenda: three flows, about 45 "
                "minutes of walkthrough plus 15 minutes for questions. Reply with "
                "whichever slot works and I'll send the invite today."
            ),
        }
    ],
}

# --- 2) Gemini parse (one real API call, same as the app does) -------------- #
try:
    details = calendar_engine.parse_meeting_request(thread)
except Exception as exc:  # noqa: BLE001 — mirror the app's catch-all
    details = {"parsing_error": f"unexpected: {exc}"}
print("\n[2] parse_meeting_request ->")
print(json.dumps(details, indent=2, ensure_ascii=False))

if details.get("parsing_error"):
    print(
        ">>> Parse FAILED — the app would show the 'could not parse' error, "
        "not the no-slot warning."
    )
    proposed, duration = [], 60
else:
    proposed = details.get("proposed_times") or []
    duration = details.get("duration_minutes") or 30
    if not proposed:
        print(">>> proposed_times is EMPTY — find_free_slot() returns None "
              "without any calendar call: that alone reproduces the warning.")
    for t in proposed:
        try:
            start = datetime.fromisoformat(
                t[:-1] + "+00:00" if t.endswith(("Z", "z")) else t
            )
            state = (
                "IN THE PAST"
                if start <= now
                else f"in {int((start - now).total_seconds() // 3600)}h"
            )
            print(f"    {t} -> {state}")
        except ValueError:
            print(f"    {t} -> UNPARSEABLE (find_free_slot skips it)")

# --- 3) Primary calendar identity (proves auth + tells us the home TZ) ------ #
service = calendar_engine._build_calendar_service()
cal = service.calendars().get(calendarId="primary").execute()
print(
    f"\n[3] primary calendar OK: id={cal.get('id')} "
    f"timeZone={cal.get('timeZone')} summary={cal.get('summary')!r}"
)


def freebusy_raw(time_min: datetime, time_max: datetime):
    """Direct FreeBusy query; returns the raw dict or the exception text."""
    body = {
        "timeMin": time_min.astimezone(timezone.utc).isoformat(),
        "timeMax": time_max.astimezone(timezone.utc).isoformat(),
        "items": [{"id": "primary"}],
    }
    try:
        return service.freebusy().query(body=body).execute()
    except Exception as exc:  # noqa: BLE001 — surfaced, never swallowed
        return f"{type(exc).__name__}: {exc}"


# --- 4) FreeBusy per proposed time (raw, errors visible) -------------------- #
print("\n[4] FreeBusy per parsed proposed time (raw response):")
for slot in proposed:
    start = datetime.fromisoformat(
        slot[:-1] + "+00:00" if slot.endswith(("Z", "z")) else slot
    )
    end = start + timedelta(minutes=int(duration))
    raw = freebusy_raw(start, end)
    print(f"  {slot}  [{start.isoformat()} .. {end.isoformat()}]")
    if isinstance(raw, str):
        print(f"    RAISED -> {raw}")
    else:
        entry = (raw.get("calendars") or {}).get("primary") or {}
        print(
            f"    busy={json.dumps(entry.get('busy', []))} "
            f"errors={json.dumps(entry.get('errors', []))}"
        )

# --- 5) find_free_slot replay (what the app actually got) ------------------- #
print(
    f"\n[5] find_free_slot(proposed, {duration}) -> "
    f"{calendar_engine.find_free_slot(proposed, duration)!r}"
)

# --- 6) Broad window for context: what is on the calendar at all? ----------- #
raw = freebusy_raw(now, now + timedelta(days=7))
print("\n[6] FreeBusy for the NEXT 7 DAYS (context):")
if isinstance(raw, str):
    print(f"    RAISED -> {raw}")
else:
    entry = (raw.get("calendars") or {}).get("primary") or {}
    busy = entry.get("busy", [])
    print(f"    {len(busy)} busy interval(s), errors={entry.get('errors')}")
    for b in busy[:25]:
        print(f"      {b.get('start')} .. {b.get('end')}")

print("\nDone — read-only calls only, nothing was created or emailed.")

