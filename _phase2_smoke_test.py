"""Temporary smoke test for Phase 2 (Draft Generation), Phase 3 (Approval
Gate) and Phase 4 (Export Proof) in app.py.

Runs the app under Streamlit's AppTest harness:
  1. Empty state  — Draft Generation with nothing triaged shows the warning.
  2. Seeded state — pre-stored drafts render side-by-side + Approval Gate pointer.
  3. Generate click — stubbed draft_reply exercises the real button path
     (progress bar, session-state writes, pair rendering) with no LLM tokens.

Run:  .venv\\Scripts\\python.exe _phase2_smoke_test.py
"""

from streamlit.testing.v1 import AppTest

import draft_machine  # same process as AppTest -> monkeypatching works

# Mirror draft_machine's _ensure_utf8_output: Windows consoles default to a
# legacy codepage while LLM error text can contain arbitrary Unicode.
import sys
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

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
THREAD_B = {
    "id": "sample-1",
    "subject": "Re: Vendorly rollout — go-live date & 500-seat quote",
    "messages": [
        {
            "from": "Sam Chen <sam.chen@vendorly.io>",
            "date": "Mon, 7 Sep 2026",
            "body": "What go-live date should we plan around?",
        },
    ],
}
ITEM_B = {
    "id": "sample-1",
    "sender": "Sam Chen <sam.chen@vendorly.io>",
    "subject": "Re: Vendorly rollout — go-live date & 500-seat quote",
    "snippet": "What go-live date should we plan around?",
    "priority": "needs-reply",
    "action": "reply this week",
}
THREAD_C = {
    "id": "sample-2",
    "subject": "Design review: new onboarding flow — Thu Sep 10?",
    "messages": [
        {
            "from": "Hana K <hana@northwind-labs.com>",
            "date": "Mon, 7 Sep 2026",
            "body": "Which slot works — Thu 14:00 or Fri 11:00?",
        },
    ],
}
ITEM_C = {
    "id": "sample-2",
    "sender": "Hana K <hana@northwind-labs.com>",
    "subject": "Design review: new onboarding flow — Thu Sep 10?",
    "snippet": "Which slot works — Thu 14:00 or Fri 11:00?",
    "priority": "needs-reply",
    "action": "reply today",
}


def boot() -> AppTest:
    at = AppTest.from_file("app.py", default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def test_empty_state() -> None:
    at = boot()
    at.button(key="nav_1").click().run()
    assert not at.exception, at.exception
    assert any("Draft Generation" in h.value for h in at.header), [h.value for h in at.header]
    assert at.warning, "expected the 'no actionable threads' warning"
    print("PASS 1/12 empty state (warning + header)")


def test_seeded_drafts() -> None:
    at = boot()
    at.session_state["threads"] = [THREAD_A]
    at.session_state["triaged"] = [ITEM_A]
    at.session_state["drafts"] = {"sample-0": DRAFT_REC}
    at.button(key="nav_1").click().run()
    assert not at.exception, at.exception
    assert len(at.expander) == 2, f"expected 2 expanders (thread + draft), got {len(at.expander)}"
    md_values = [m.value for m in at.markdown]
    assert any("Budget needs your sign-off" in v for v in md_values), md_values
    assert any(DRAFT_REC["draft"] in v for v in md_values), md_values
    assert at.success and "Approval Gate" in at.success[0].value, [
        s.value for s in at.success
    ]
    print("PASS 2/12 seeded drafts render side-by-side + Approval Gate pointer")


def test_generate_click() -> None:
    at = boot()
    at.session_state["threads"] = [THREAD_A]
    at.session_state["triaged"] = [ITEM_A]
    at.button(key="nav_1").click().run()

    original = draft_machine.draft_reply
    draft_machine.draft_reply = lambda thread: f"STUB REPLY to: {thread['subject']}"
    try:
        at.button(key="generate_all_drafts_btn").click().run()
    finally:
        draft_machine.draft_reply = original

    assert not at.exception, at.exception
    drafts = at.session_state["drafts"]
    assert "sample-0" in drafts, drafts
    assert drafts["sample-0"]["draft"].startswith("STUB REPLY"), drafts
    assert drafts["sample-0"]["reply_to"] == "Meera I <m@acme.com>", drafts
    assert drafts["sample-0"]["model"], drafts
    assert len(at.expander) == 2, f"expected 2 expanders, got {len(at.expander)}"
    md_values = [m.value for m in at.markdown]
    assert any("STUB REPLY" in v for v in md_values), md_values
    assert at.success and "Approval Gate" in at.success[0].value, [
        s.value for s in at.success
    ]
    print("PASS 3/12 Generate click stores draft + renders pair + pointer")


def test_real_generate_error_path() -> None:
    """No stub: let the real draft_reply run. Both LLM backends currently
    fail fast (OpenRouter 402 no credits / Gemini 429 rate limit), costing
    nothing — this proves a failing thread surfaces a per-thread st.error
    and the retry prompt instead of crashing the phase."""
    at = boot()
    at.session_state["threads"] = [THREAD_A]
    at.session_state["triaged"] = [ITEM_A]
    at.button(key="nav_1").click().run()

    at.button(key="generate_all_drafts_btn").click().run()
    assert not at.exception, at.exception
    errors = [e.value for e in at.error]
    drafts = at.session_state["drafts"]
    if "sample-0" in drafts:
        print("PASS 4/12 real generate path produced a draft (a backend had capacity)")
        return
    # Zero drafts succeeded -> per-thread error + retry info, no warning, no success.
    assert errors and "Draft failed" in errors[0], errors
    assert any("Click **Generate All Drafts**" in i.value for i in at.info), [
        i.value for i in at.info
    ]
    assert not at.success, "success must not show when no draft exists"
    print("PASS 4/12 real backend failure degrades gracefully (error + retry info)")


def test_partial_failure() -> None:
    """One thread drafts, one fails -> pair renders for the drafted thread,
    per-thread error for the other, 'still have no draft' warning, no success."""
    at = boot()
    at.session_state["threads"] = [THREAD_A, THREAD_B]
    at.session_state["triaged"] = [ITEM_A, ITEM_B]
    at.button(key="nav_1").click().run()

    original = draft_machine.draft_reply

    def flaky_draft(thread: dict) -> str:
        if "Vendorly" in thread["subject"]:
            raise RuntimeError("boom: backend down")
        return f"STUB REPLY to: {thread['subject']}"

    draft_machine.draft_reply = flaky_draft
    try:
        at.button(key="generate_all_drafts_btn").click().run()
    finally:
        draft_machine.draft_reply = original

    assert not at.exception, at.exception
    drafts = at.session_state["drafts"]
    assert list(drafts) == ["sample-0"], drafts
    errors = [e.value for e in at.error]
    assert any("Vendorly" in e and "boom: backend down" in e for e in errors), errors
    assert any("still have no draft" in w.value for w in at.warning), [
        w.value for w in at.warning
    ]
    assert not at.success, "success must not show while drafts are missing"
    assert len(at.expander) == 2, "only the drafted thread should render a pair"
    print("PASS 5/12 partial failure: warning shown, no success, one pair rendered")


def test_approval_empty_state() -> None:
    at = boot()
    at.button(key="nav_2").click().run()
    assert not at.exception, at.exception
    assert any("Approval Gate" in h.value for h in at.header), [h.value for h in at.header]
    assert at.warning and "No drafts to review" in at.warning[0].value, [
        w.value for w in at.warning
    ]
    print("PASS 6/12 Approval Gate empty state (warning)")


def test_approval_flow() -> None:
    at = boot()
    at.session_state["threads"] = [THREAD_A, THREAD_B, THREAD_C]
    at.session_state["triaged"] = [ITEM_A, ITEM_B, ITEM_C]
    at.session_state["drafts"] = {
        "sample-0": dict(DRAFT_REC),
        "sample-1": {
            "draft": "Holding at 3% for one year — let's close by Friday.",
            "model": "stub-model",
            "subject": ITEM_B["subject"],
            "reply_to": ITEM_B["sender"],
        },
        "sample-2": {
            "draft": "Thursday 14:00 works — send the invite.",
            "model": "stub-model",
            "subject": ITEM_C["subject"],
            "reply_to": ITEM_C["sender"],
        },
    }
    at.button(key="nav_2").click().run()
    assert not at.exception, at.exception
    assert [m.value for m in at.metric] == ["0", "0", "3", "0"], [m.value for m in at.metric]
    assert len(at.text_area) == 3, len(at.text_area)

    # Edit the first draft in its text box, then approve — the edit is what's saved.
    at.text_area(key="draft_text_sample-0").set_value("EDITED: take the 2.5% two-year uplift.")
    at.run()
    at.button(key="approve_sample-0").click().run()
    assert not at.exception, at.exception
    approved = at.session_state["approved"]
    assert approved["sample-0"]["draft"] == "EDITED: take the 2.5% two-year uplift.", approved
    assert approved["sample-0"]["edited"] is True, approved
    assert approved["sample-0"]["status"] == "ready_to_send", approved
    assert approved["sample-0"]["thread_subject"] == ITEM_A["subject"], approved
    assert [m.value for m in at.metric] == ["1", "0", "2", "0"], [m.value for m in at.metric]

    # Approve the second one untouched — edited flag must be False.
    at.button(key="approve_sample-1").click().run()
    assert not at.exception, at.exception
    assert at.session_state["approved"]["sample-1"]["edited"] is False
    assert [m.value for m in at.metric] == ["2", "0", "1", "0"], [m.value for m in at.metric]

    # Reject the third — everything reviewed: balloons state + Export Proof pointer.
    at.button(key="reject_sample-2").click().run()
    assert not at.exception, at.exception
    assert at.session_state["rejected"] == {"sample-2"}, at.session_state["rejected"]
    assert [m.value for m in at.metric] == ["2", "1", "0", "0"], [m.value for m in at.metric]
    assert at.success and "Export Proof" in at.success[0].value, [s.value for s in at.success]
    assert any("Reviewed decisions" in s.value for s in at.subheader), [
        s.value for s in at.subheader
    ]
    assert len(at.expander) == 3, "one expander per decided record"
    assert len(at.text_area) == 0, "no pending editors remain"
    print("PASS 7/12 approve edited + approve as-is + reject -> counts, balloons, pointer")


def test_regenerate() -> None:
    at = boot()
    at.session_state["threads"] = [THREAD_A]
    at.session_state["triaged"] = [ITEM_A]
    at.session_state["drafts"] = {"sample-0": dict(DRAFT_REC)}
    at.button(key="nav_2").click().run()

    original = draft_machine.draft_reply
    draft_machine.draft_reply = lambda thread: f"FRESH STUB: {thread['subject']}"
    try:
        at.button(key="regen_sample-0").click().run()
    finally:
        draft_machine.draft_reply = original

    assert not at.exception, at.exception
    assert at.session_state["drafts"]["sample-0"]["draft"].startswith("FRESH STUB"), (
        at.session_state["drafts"]
    )
    assert at.text_area(key="draft_text_sample-0").value.startswith("FRESH STUB"), (
        "editor must show the fresh draft"
    )
    assert not at.session_state["approved"] and not at.session_state["rejected"]
    assert [m.value for m in at.metric] == ["0", "0", "1", "0"], [m.value for m in at.metric]
    print("PASS 8/12 Regenerate swaps a fresh draft into the editor")


def test_regenerate_failure() -> None:
    at = boot()
    at.session_state["threads"] = [THREAD_A]
    at.session_state["triaged"] = [ITEM_A]
    at.session_state["drafts"] = {"sample-0": dict(DRAFT_REC)}
    at.button(key="nav_2").click().run()

    original = draft_machine.draft_reply

    def _boom(thread: dict) -> str:
        raise RuntimeError("boom: backend down")

    draft_machine.draft_reply = _boom
    try:
        at.button(key="regen_sample-0").click().run()
    finally:
        draft_machine.draft_reply = original

    assert not at.exception, at.exception
    errors = [e.value for e in at.error]
    assert any("Regenerate failed" in e and "boom: backend down" in e for e in errors), errors
    assert at.session_state["drafts"]["sample-0"]["draft"] == DRAFT_REC["draft"], (
        "draft must be unchanged on failure"
    )
    assert not at.session_state["approved"] and not at.session_state["rejected"]
    print("PASS 9/12 Regenerate failure surfaces inside the card, draft intact")


def test_export_empty_state() -> None:
    at = boot()
    at.button(key="nav_3").click().run()
    assert not at.exception, at.exception
    assert any("Export Proof" in h.value for h in at.header), [h.value for h in at.header]
    assert at.warning and "Nothing to export yet" in at.warning[0].value, [
        w.value for w in at.warning
    ]
    print("PASS 10/12 Export Proof empty state (warning)")


APPROVED_RECORD_A = {
    "approved_at": "2026-09-07T21:30:00+02:00",
    "status": "ready_to_send",
    "edited": True,
    "id": "sample-0",
    "thread_subject": ITEM_A["subject"],
    "reply_to": ITEM_A["sender"],
    "model": "stub-model",
    "draft": "Edited reply with <html> tags & symbols.",
    "priority": "urgent",
}


def test_export_ui() -> None:
    at = boot()
    at.session_state["threads"] = [THREAD_A]
    at.session_state["triaged"] = [ITEM_A]
    at.session_state["approved"] = {"sample-0": dict(APPROVED_RECORD_A)}
    at.session_state["rejected"] = {"sample-2"}
    at.button(key="nav_3").click().run()
    assert not at.exception, at.exception
    assert len(at.expander) == 2, f"1 pair x (thread + draft), got {len(at.expander)}"
    assert at.download_button(key="download_proof_md"), "markdown download button missing"
    assert at.download_button(key="download_proof_html"), "html download button missing"
    assert at.info and "approved draft(s) ready to export" in at.info[0].value, [
        i.value for i in at.info
    ]
    print("PASS 11/12 Export preview renders pair + both download buttons")


def test_export_content() -> None:
    """generate_proof_markdown/html run inside an AppTest context via a tiny
    wrapper that imports app and stashes the built documents in session state."""
    wrapper = (
        "import app\n"
        "import streamlit as st\n"
        "st.session_state['_proof_md'] = app.generate_proof_markdown()\n"
        "st.session_state['_proof_html'] = app.generate_proof_html()\n"
    )
    at = AppTest.from_string(wrapper, default_timeout=60)
    at.session_state["threads"] = [THREAD_A]
    at.session_state["triaged"] = [ITEM_A]
    at.session_state["approved"] = {"sample-0": dict(APPROVED_RECORD_A)}
    at.session_state["rejected"] = {"sample-2"}
    at.run()
    assert not at.exception, at.exception
    md = at.session_state["_proof_md"]
    html_doc = at.session_state["_proof_html"]

    # Markdown: title, date, quoted thread, fenced reply.
    assert md.startswith("# The Draft Desk — Proof of Work"), md[:200]
    assert "**Date:**" in md
    assert "draft(s) approved at the Approval Gate" in md and "rejected" in md
    assert ITEM_A["subject"] in md
    assert "### Original thread" in md and "> **✉️ Priya S <p@acme.com>**" in md
    assert "### Approved reply" in md and "```text" in md
    assert "Edited reply with <html> tags & symbols." in md
    assert "*(edited before approval)*" in md

    # HTML: dark theme, grid, orange/green borders, escaping.
    assert "#1a1a2e" in html_doc
    assert "grid-template-columns: 1fr 1fr" in html_doc
    assert "border: 2px solid #ff9f43" in html_doc
    assert "border: 2px solid #2ed573" in html_doc
    assert "Edited reply with &lt;html&gt; tags &amp; symbols." in html_doc
    assert ITEM_A["subject"] in html_doc
    assert 'class="badge">edited<' in html_doc
    print("PASS 12/12 proof documents: markdown quotes + fence, styled dark HTML, escaping")


if __name__ == "__main__":
    test_empty_state()
    test_seeded_drafts()
    test_generate_click()
    test_real_generate_error_path()
    test_partial_failure()
    test_approval_empty_state()
    test_approval_flow()
    test_regenerate()
    test_regenerate_failure()
    test_export_empty_state()
    test_export_ui()
    test_export_content()
    print("ALL SMOKE TESTS PASSED (Phase 2 + 3 + 4)")
