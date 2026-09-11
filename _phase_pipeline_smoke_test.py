"""Smoke test for the Run Full Pipeline capability (app.py).

Runs the app under Streamlit's AppTest harness with the triage/LLM/draft
layer stubbed, so nothing touches the network and no LLM key is needed:

  1. Happy path - the pipeline button runs fetch(sample) -> triage(stub) ->
     draft(stub) with a live st.status, lands on the Approval Gate with the
     drafts stored, pipeline_log filled and pipeline_running False.
  2. Rerun continuity - the phase survives a follow-up run (no dangling
     pipeline_running).

Run:  .venv\\Scripts\\python.exe _phase_pipeline_smoke_test.py
"""

from __future__ import annotations

import json
import sys

from streamlit.testing.v1 import AppTest

import app  # same process as AppTest -> constants resolve
import draft_machine
import llm_client
import triage

# Mirror the other smoke tests: Windows consoles default to a legacy codepage
# while assert text can contain arbitrary Unicode.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# --- Stub plumbing (in place BEFORE the first AppTest run) ------------------ #

_PRIORITIES = ["urgent", "needs-reply", "fyi", "fyi", "ignore"]


def _stub_triage_inbox(threads: list) -> list:
    return [dict(t, priority=_PRIORITIES[i % len(_PRIORITIES)]) for i, t in enumerate(threads)]


def _stub_draft_reply(thread: dict) -> str:
    return f"Stub draft for: {thread.get('subject', '')}"


triage.triage_inbox = _stub_triage_inbox  # type: ignore[assignment]
draft_machine.draft_reply = _stub_draft_reply  # type: ignore[assignment]
llm_client.configured_backends = lambda: ["stub-backend"]  # type: ignore[assignment]


def _boot() -> AppTest:
    at = AppTest.from_file("app.py", default_timeout=60)
    at.session_state["source"] = app.SOURCE_SAMPLE
    at.run()
    assert not at.exception, at.exception
    return at


# --- Tests ------------------------------------------------------------------ #


def test_full_pipeline_happy_path() -> None:
    at = _boot()

    # The ⚡ button now lives at the top of the sidebar, with its caption
    # right below it (both asserted before the click).
    assert at.button(key="run_full_pipeline_btn"), "sidebar pipeline button missing"
    assert any("Fetches, triages, and drafts" in c.value for c in at.caption), [
        c.value for c in at.caption
    ]

    at.button(key="run_full_pipeline_btn").click().run()
    assert not at.exception, at.exception

    # Session state: the run landed on the gate with everything in place.
    assert at.session_state["current_phase"] == "Approval Gate", at.session_state["current_phase"]
    assert at.session_state["pipeline_running"] is False, "pipeline_running must be False"
    assert len(at.session_state["drafts"]) == 2, at.session_state["drafts"]
    assert at.session_state["approved"] == {}, at.session_state["approved"]
    assert at.session_state["rejected"] == set(), at.session_state["rejected"]
    assert at.session_state["sent"] == set(), at.session_state["sent"]
    assert at.session_state["booked"] == {}, at.session_state["booked"]

    # The two actionable sample threads got stub drafts (ids come from the
    # sample file itself — real Gmail-style ids, not "sample-N").
    sample_ids = [
        t["id"]
        for t in json.loads(app.SAMPLE_THREADS_PATH.read_text(encoding="utf-8"))
        if isinstance(t, dict) and t.get("id")
    ][:2]
    assert sorted(at.session_state["drafts"]) == sorted(sample_ids), at.session_state["drafts"]
    rec = at.session_state["drafts"][sample_ids[0]]
    assert rec["draft"].startswith("Stub draft for: P1 incident"), rec
    assert rec["model"] == draft_machine.MODEL, rec
    assert rec["reply_to"], rec

    # The log mirrors the run: fetch -> triage -> 2 drafts -> complete.
    log = at.session_state["pipeline_log"]
    assert log[0] == "Fetched 5 thread(s) from sample_threads.json.", log[0]
    assert "Triaged 5 thread(s): 1 urgent, 1 needs-reply." in log[1], log[1]
    assert log[2].startswith("Draft 1/2: P1 incident") and log[2].endswith("— done"), log[2]
    assert log[3].startswith("Draft 2/2: Q3 self-review") and log[3].endswith("— done"), log[3]
    assert log[-1] == "Pipeline complete! 2 draft(s) ready for review.", log[-1]

    # NOTE: the st.status container is transient — the run ends with
    # st.rerun(), so the final tree shows the Approval Gate, not the status.
    # Its outcomes are asserted via pipeline_log above.

    # The gate phase is what renders now (Approval Gate header visible).
    assert any("Approval Gate" in h.value for h in at.header), [h.value for h in at.header]
    print("PASS 1/2 Full pipeline: fetched->triaged->drafted 2, gate phase + complete status")


def test_rerun_continuity() -> None:
    at = _boot()
    at.button(key="run_full_pipeline_btn").click().run()
    assert not at.exception, at.exception

    # A follow-up run keeps the gate phase with no dangling pipeline_running.
    at.run()
    assert not at.exception, at.exception
    assert at.session_state["current_phase"] == "Approval Gate"
    assert at.session_state["pipeline_running"] is False
    assert len(at.session_state["drafts"]) == 2
    print("PASS 2/2 Rerun continuity: phase kept, pipeline_running stays False")


def test_export_shows_action_log() -> None:
    import task_logger

    at = _boot()
    at.button(key="run_full_pipeline_btn").click().run()
    assert not at.exception, at.exception

    # Export requires at least one APPROVED draft (the pipeline only drafts —
    # approvals happen at the gate). Seed one so the page renders fully.
    sample_ids = [
        t["id"]
        for t in json.loads(app.SAMPLE_THREADS_PATH.read_text(encoding="utf-8"))
        if isinstance(t, dict) and t.get("id")
    ]
    at.session_state["approved"] = {
        sample_ids[0]: {
            "approved_at": "2026-09-10T09:00:00+02:00",
            "status": "ready_to_send",
            "edited": False,
            "id": sample_ids[0],
            "thread_subject": "Kickoff call — when works for you?",
            "reply_to": "Sam Chen <sam.chen@vendorly.io>",
            "model": "stub-model",
            "draft": "Thursday 2pm works great.",
            "priority": "urgent",
        }
    }

    # Empty state: nothing sent/booked yet. (Clear the log first — a previous
    # aborted run may have left seeded records behind.)
    if task_logger.LOG_PATH.exists():
        task_logger.LOG_PATH.unlink()
    at.button(key="nav_3").click().run()
    assert not at.exception, at.exception
    assert any(s.value == "Action Log" for s in at.subheader), [s.value for s in at.subheader]
    assert any("No actions logged yet." in i.value for i in at.info), [i.value for i in at.info]

    # Populated state: seed one sent + one booked record and re-render.
    if task_logger.LOG_PATH.exists():
        task_logger.LOG_PATH.unlink()
    task_logger.log_action(
        "sent", "Kickoff call — when works for you?", "sam.chen@vendorly.io", "stub-msg-001"
    )
    task_logger.log_action(
        "booked",
        "Design review: new onboarding flow — Thu Sep 10?",
        "Design review: new onboarding flow",
        "stub-event-001",
    )
    at.run()
    assert not at.exception, at.exception
    md = " ".join(m.value for m in at.markdown)
    assert "📨 **SENT**" in md, md
    assert "📅 **BOOKED**" in md, md
    assert "`sam.chen@vendorly.io`" in md, md
    assert "`Design review: new onboarding flow`" in md, md
    caps = " | ".join(c.value for c in at.caption)
    assert ("AM" in caps or "PM" in caps), caps  # "%b %d %I:%M %p" timestamps
    if task_logger.LOG_PATH.exists():
        task_logger.LOG_PATH.unlink()  # remove the seeded audit artifact
    print("PASS 3/3 Export Proof: Action Log renders empty + populated states")


def test_html_proof_contains_action_log() -> None:
    """The ⬇️ Download Proof (HTML) artifact must include the Action Log."""
    import task_logger

    # The action log from the previous test was cleaned up — re-seed it.
    if task_logger.LOG_PATH.exists():
        task_logger.LOG_PATH.unlink()
    task_logger.log_action(
        "sent", "Kickoff call — when works for you?", "sam.chen@vendorly.io", "stub-msg-001"
    )
    task_logger.log_action(
        "booked",
        "Design review: new onboarding flow — Thu Sep 10?",
        "Design review: new onboarding flow",
        "stub-event-001",
    )

    # generate_proof_html() reads st.session_state, so run it inside a
    # from_string runner with the same session state the app would have.
    runner = (
        "import app\n"
        "html = app.generate_proof_html()\n"
        "open('_proof_out.html', 'w', encoding='utf-8').write(html)\n"
        "print('PROOF_BYTES', len(html))\n"
    )
    at2 = AppTest.from_string(runner)
    at2.session_state["approved"] = {
        "19b7e2a4c8f10d53": {
            "approved_at": "2026-09-10T09:00:00+02:00",
            "status": "sent",
            "edited": False,
            "id": "19b7e2a4c8f10d53",
            "thread_subject": "Kickoff call — when works for you?",
            "reply_to": "Sam Chen <sam.chen@vendorly.io>",
            "model": "stub-model",
            "draft": "Thursday 2pm works great.",
            "priority": "urgent",
        }
    }
    at2.session_state["rejected"] = set()
    at2.session_state["threads"] = json.loads(
        app.SAMPLE_THREADS_PATH.read_text(encoding="utf-8")
    )
    at2.session_state["triaged"] = []
    at2.run()
    assert not at2.exception, at2.exception

    proof = open("_proof_out.html", encoding="utf-8").read()
    assert "📋 Action Log — actions taken from the Approval Gate" in proof
    assert "📨 SENT" in proof
    assert "📅 BOOKED" in proof
    assert "Kickoff call — when works for you?" in proof
    assert "sam.chen@vendorly.io" in proof
    assert "Design review: new onboarding flow" in proof
    assert "stub-msg-001" in proof and "stub-event-001" in proof

    import os

    os.remove("_proof_out.html")
    if task_logger.LOG_PATH.exists():
        task_logger.LOG_PATH.unlink()
    print("PASS 4/4 Download Proof (HTML) contains the Action Log section")


if __name__ == "__main__":
    try:
        test_full_pipeline_happy_path()
        test_rerun_continuity()
        test_export_shows_action_log()
        test_html_proof_contains_action_log()
    except Exception:
        import traceback

        traceback.print_exc(file=sys.stdout)
        sys.exit(1)
    print()
    print("All full-pipeline smoke checks passed (no LLM key or Gmail was used).")
