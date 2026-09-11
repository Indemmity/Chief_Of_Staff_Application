"""Smoke test for the Approval Gate Send capability (app.py + engine.send_reply).

Runs the app under Streamlit's AppTest harness with engine.send_reply stubbed,
so nothing touches the network and no real email is sent:

  1. Send happy path — approve a seeded draft, click 📨 Send reply; the stub
     returns a Gmail-style response. Asserts the stub captured the extracted
     recipient ("Name <email>" header), the approved record flips to status
     "sent", the thread id joins the sent set, the success message + 📨 Sent
     badge render, the Send button disappears, and the metrics gain a 4th
     column with the sent count.
  2. Send failure path — the stub raises; asserts the error surfaces inside
     the reviewed-decisions card, the draft stays approved-but-unsent, and
     the Send button remains for a retry.
  3. No-recipient path — the thread's last message has no extractable
     address; asserts the guard fires before send_reply is ever called.

Run:  .venv\\Scripts\\python.exe _phase3_send_smoke_test.py
"""

from __future__ import annotations

import sys

from streamlit.testing.v1 import AppTest

import engine  # same process as AppTest -> monkeypatching works
import task_logger  # the Send flow now audits to action_log.json

# Mirror _phase2_smoke_test: Windows consoles default to a legacy codepage
# while error text can contain arbitrary Unicode.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# --- Stub plumbing ---------------------------------------------------------- #
# app._get_send_reply() is @st.cache_resource and does `from engine import
# send_reply` on its first call, so the stub must be in place BEFORE the first
# AppTest run. The dispatcher indirection lets each test swap behaviour even
# though the cache holds one resolved callable for the whole process.

_SENT_CALLS: list[dict] = []


def _happy_stub(thread_id: str, to: str, subject: str, body: str, message_id=None) -> dict:
    _SENT_CALLS.append(
        {"thread_id": thread_id, "to": to, "subject": subject, "body": body}
    )
    return {"message_id": "stub-msg-001", "thread_id": thread_id, "status": "sent"}


def _boom_stub(thread_id: str, to: str, subject: str, body: str, message_id=None) -> dict:
    raise RuntimeError("boom: SMTP down")


_CURRENT = {"fn": _happy_stub}

engine.send_reply = lambda *args, **kwargs: _CURRENT["fn"](*args, **kwargs)  # type: ignore[assignment]

# --- Fixtures (mirrors _phase2_smoke_test.py) ------------------------------- #

THREAD_A = {
    "id": "sample-0",
    "subject": "Q3 Budget Review — sign-off needed by Thursday",
    "messages": [
        {"from": "Priya S <p@acme.com>", "date": "Tue, 8 Sep 2026", "body": "Adding a flag."},
        {
            "from": "Meera I <m@acme.com>",
            "date": "Tue, 8 Sep 2026",
            "body": "Budget needs your sign-off by Thursday.",
        },
    ],
}

ITEM_A = {
    "id": "sample-0",
    "sender": "Meera I <m@acme.com>",
    "subject": "Q3 Budget Review — sign-off needed by Thursday",
    "snippet": "Budget needs your sign-off by Thursday.",
    "priority": "urgent",
    "action": "reply now",
}

DRAFT_REC = {
    "draft": "Signed off — the budget is approved as listed.",
    "model": "stub-model",
    "subject": ITEM_A["subject"],
    "reply_to": ITEM_A["sender"],
}


def _gate_with_approved() -> AppTest:
    """Boot the app, seed one drafted thread, approve it at the gate."""
    at = AppTest.from_file("app.py", default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    at.session_state["threads"] = [THREAD_A]
    at.session_state["triaged"] = [ITEM_A]
    at.session_state["drafts"] = {"sample-0": dict(DRAFT_REC)}
    at.button(key="nav_2").click().run()
    assert not at.exception, at.exception
    at.button(key="approve_sample-0").click().run()
    assert not at.exception, at.exception
    assert at.session_state["approved"]["sample-0"]["status"] == "ready_to_send"
    return at


def test_send_happy_path() -> None:
    _SENT_CALLS.clear()
    _CURRENT["fn"] = _happy_stub
    # Fresh audit trail so the log assertions below are deterministic.
    if task_logger.LOG_PATH.exists():
        task_logger.LOG_PATH.unlink()
    at = _gate_with_approved()

    at.button(key="send_sample-0").click().run()
    assert not at.exception, at.exception

    # The stub captured the send call with the extracted bare recipient.
    assert len(_SENT_CALLS) == 1, _SENT_CALLS
    call = _SENT_CALLS[0]
    assert call["to"] == "m@acme.com", call  # "Meera I <m@acme.com>" -> bare address
    assert call["thread_id"] == "sample-0", call
    assert call["subject"] == ITEM_A["subject"], call
    assert call["body"] == DRAFT_REC["draft"], call

    # Session state: sent tracking + approved record flipped to "sent".
    assert at.session_state["sent"] == {"sample-0"}, at.session_state["sent"]
    rec = at.session_state["approved"]["sample-0"]
    assert rec["status"] == "sent", rec
    assert rec["sent_to"] == "m@acme.com", rec
    assert rec["gmail_message_id"] == "stub-msg-001", rec
    assert rec.get("sent_at"), rec

    # Audit trail: the successful send appended one "sent" record.
    log = task_logger.get_action_log()
    assert len(log) == 1, log
    entry = log[0]
    assert entry["action_type"] == "sent", entry
    assert entry["thread_subject"] == ITEM_A["subject"], entry
    assert entry["detail"] == "m@acme.com", entry
    assert entry["id"] == "stub-msg-001", entry
    assert entry["timestamp"], entry

    # UI: 📨 Sent badge on the expander, success message, Send button gone,
    # and the metrics row now ends with the sent count.
    assert any("📨 Sent" in e.label for e in at.expander), [e.label for e in at.expander]
    assert any("Sent to" in s.value for s in at.success), [s.value for s in at.success]
    assert not [b for b in at.button if b.key == "send_sample-0"], "Send button must disappear"
    assert [m.value for m in at.metric] == ["1", "0", "0", "1"], [m.value for m in at.metric]
    print("PASS 1/3 Send happy path: recipient extracted, record flipped, badge + success")


def test_send_failure() -> None:
    _SENT_CALLS.clear()
    _CURRENT["fn"] = _boom_stub
    at = _gate_with_approved()

    at.button(key="send_sample-0").click().run()
    assert not at.exception, at.exception

    # Nothing was sent; the error surfaces inside the reviewed-decisions card.
    assert at.session_state["sent"] == set(), at.session_state["sent"]
    assert at.session_state["approved"]["sample-0"]["status"] == "ready_to_send"
    errors = [e.value for e in at.error]
    assert any("Send failed" in e and "boom: SMTP down" in e for e in errors), errors
    # The Send button remains so the user can retry.
    assert any(b.key == "send_sample-0" for b in at.button), "Send button must remain for retry"
    assert [m.value for m in at.metric] == ["1", "0", "0", "0"], [m.value for m in at.metric]
    print("PASS 2/3 Send failure: error in card, draft stays approved-but-unsent, retry kept")


def test_send_without_recipient() -> None:
    _SENT_CALLS.clear()
    _CURRENT["fn"] = _happy_stub
    at = _gate_with_approved()

    # Replace the thread with one whose last message has no extractable address.
    bad_thread = {
        "id": "sample-0",
        "subject": ITEM_A["subject"],
        "messages": [
            {"from": "Meera (unknown address)", "date": "Tue, 8 Sep 2026", "body": "hi"}
        ],
    }
    at.session_state["threads"] = [bad_thread]
    at.button(key="send_sample-0").click().run()
    assert not at.exception, at.exception

    # The guard must fire BEFORE send_reply is called.
    assert _SENT_CALLS == [], "send_reply must not run without a recipient"
    assert at.session_state["sent"] == set(), at.session_state["sent"]
    errors = [e.value for e in at.error]
    assert any("Could not extract a recipient address" in e for e in errors), errors
    print("PASS 3/3 Send without extractable recipient: guard fires before sending")


if __name__ == "__main__":
    try:
        test_send_happy_path()
        test_send_failure()
        test_send_without_recipient()
    except Exception:
        import traceback

        traceback.print_exc(file=sys.stdout)
        sys.exit(1)
    finally:
        # Remove the test's audit-trail artifact so real usage starts fresh.
        if task_logger.LOG_PATH.exists():
            task_logger.LOG_PATH.unlink()
    print()
    print("All Send-capability smoke checks passed (no email was actually sent).")
