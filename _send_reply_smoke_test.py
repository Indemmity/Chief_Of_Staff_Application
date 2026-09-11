"""
_send_reply_smoke_test.py — offline validation of engine.send_reply().

Monkeypatches engine._build_gmail_service with a fake Gmail API service, so
nothing touches the network and no real email is sent. Verifies that
send_reply():

  1. base64url-encodes the MIMEText message into the send body's "raw" field
  2. includes threadId in the send body and targets GMAIL_USER_ID
  3. prefixes "Re: " only when the subject does not already start with it
  4. sets In-Reply-To / References when message_id is given (and not otherwise)
  5. returns {"message_id", "thread_id", "status": "sent"}
  6. imports engine.py without pulling googleapiclient in (lazy import)

Run with the project venv:  .venv\\Scripts\\python.exe _send_reply_smoke_test.py
"""

from __future__ import annotations

import base64
import sys
from email import message_from_bytes
from email.header import decode_header
from email.policy import default as default_policy

import engine


# --------------------------------------------------------------------------- #
# Fake Gmail API service — captures the send call instead of performing it
# --------------------------------------------------------------------------- #


class _FakeSend:
    def __init__(self, capture: dict) -> None:
        self._capture = capture

    def execute(self) -> dict:
        return {
            "id": "fake-msg-001",
            "threadId": self._capture["body"].get("threadId", "fake-thread-999"),
        }


class _FakeMessages:
    def __init__(self, capture: dict) -> None:
        self._capture = capture

    def send(self, userId: str, body: dict) -> _FakeSend:
        self._capture["userId"] = userId
        self._capture["body"] = body
        return _FakeSend(self._capture)


class _FakeUsers:
    def __init__(self, capture: dict) -> None:
        self._capture = capture

    def messages(self) -> _FakeMessages:
        return _FakeMessages(self._capture)


class _FakeService:
    def __init__(self, capture: dict) -> None:
        self._capture = capture

    def users(self) -> _FakeUsers:
        return _FakeUsers(self._capture)


def _header(msg, name: str) -> str | None:
    """Decoded header value under the default policy (handles RFC2047)."""
    value = msg.get(name)
    if value is None:
        return None
    return "".join(
        part.decode(enc or "utf-8") if isinstance(part, bytes) else part
        for part, enc in decode_header(value)
    )


def main() -> int:
    engine._ensure_utf8_output()

    capture: dict = {}
    engine._build_gmail_service = lambda: _FakeService(capture)  # type: ignore[method-assign]

    failures: list[str] = []

    def check(label: str, ok: bool) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    # -- Case 1: full reply with message_id ---------------------------------- #
    print("Case 1: reply with message_id")
    result = engine.send_reply(
        thread_id="1a06f3da0428c156",
        to="priya.sharma@acme.com",
        subject="Q3 Budget Review — sign-off needed",
        body="Approved on my side — sign-off attached.\n\n— The Chief of Staff",
        message_id="<caf-123@acme.com>",
    )

    check(
        "userId == GMAIL_USER_ID ('me')",
        capture.get("userId") == engine.GMAIL_USER_ID == "me",
    )
    body = capture.get("body", {})
    check("send body carries threadId", body.get("threadId") == "1a06f3da0428c156")

    raw = body.get("raw", "")
    try:
        decoded = base64.urlsafe_b64decode(raw)
        msg = message_from_bytes(decoded, policy=default_policy)
        parse_ok = True
    except Exception as exc:  # noqa: BLE001 — reported as a failing check
        print(f"      (decode error: {exc})")
        parse_ok, msg = False, None
    check("raw field is valid base64url MIME", parse_ok and msg is not None)

    if msg is not None:
        check("To header set", _header(msg, "To") == "priya.sharma@acme.com")
        check(
            "Subject gets 'Re: ' prefix",
            _header(msg, "Subject") == "Re: Q3 Budget Review — sign-off needed",
        )
        check(
            "In-Reply-To header set",
            _header(msg, "In-Reply-To") == "<caf-123@acme.com>",
        )
        check("References header set", _header(msg, "References") == "<caf-123@acme.com>")
        payload = msg.get_payload(decode=True)
        text = payload.decode("utf-8") if payload else ""
        check("body text preserved (Unicode-safe)", "sign-off attached" in text)

    check(
        "return dict shape {message_id, thread_id, status}",
        result
        == {
            "message_id": "fake-msg-001",
            "thread_id": "1a06f3da0428c156",
            "status": "sent",
        },
    )

    # -- Case 2: subject already has 'RE:', no message_id -------------------- #
    print("Case 2: 'RE:' subject, no message_id")
    capture.clear()
    result2 = engine.send_reply(
        thread_id="t-2",
        to="b@x.com",
        subject="RE: already a reply",
        body="second case",
    )
    msg2 = message_from_bytes(
        base64.urlsafe_b64decode(capture["body"]["raw"]), policy=default_policy
    )
    check("no double 'Re:' prefix", _header(msg2, "Subject") == "RE: already a reply")
    check("In-Reply-To omitted when message_id is None", msg2.get("In-Reply-To") is None)
    check("References omitted when message_id is None", msg2.get("References") is None)
    check("response threadId echoed into result", result2["thread_id"] == "t-2")
    check("status is 'sent'", result2["status"] == "sent")

    # -- Case 3: lazy Google imports ----------------------------------------- #
    print("Case 3: engine.py stays google-free on import")
    check(
        "googleapiclient not imported at module import time",
        "googleapiclient" not in sys.modules,
    )

    print()
    if failures:
        print(f"FAILED — {len(failures)} check(s): {failures}")
        return 1
    print("All send_reply smoke checks passed (no email was sent).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
