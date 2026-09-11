"""
app.py — The Draft Desk: unified Streamlit front-end for the Chief-of-Staff
pipeline.

Run from the project root:

    streamlit run app.py

Phases
------
1. Inbox & Triage    Pull threads (Gmail via engine.py, or sample_threads.json
                     as the demo fallback) and classify them with triage.py.
2. Draft Generation  Draft replies for every actionable (urgent +
                     needs-reply) thread via draft_machine.draft_reply().
3. Approval Gate     Approve, edit, regenerate, or reject each draft at the
                     gate; send approved replies, and for meeting-request
                     threads parse the request and book the first free slot.
4. Export Proof      Preview approved drafts and export the proof of work
                     as Markdown or styled, shareable HTML.

Data shapes
-----------
engine.fetch_threads() returns the "engine format":

    [{"thread_id", "sender", "subject", "snippet", "date"}]

The rest of the pipeline (context_builder.py, draft_machine.py) speaks the
"pipeline format":

    [{"id", "subject", "messages": [{"from", "date", "body"}]}]

engine_to_pipeline() bridges the two; the snippet stands in as the body for
now (full message fetch can be added later). pipeline_to_triage_format()
flattens the pipeline format back to the {"sender", "subject", "snippet"}
shape that triage.triage_inbox() expects.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from task_logger import get_action_log, log_action  # audit trail: action_log.json

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #

WORKSPACE_DIR = Path(__file__).resolve().parent
SAMPLE_THREADS_PATH = WORKSPACE_DIR / "sample_threads.json"

# engine.py truncates snippets to 200 chars before returning them; we apply
# the same cap to sample bodies so triage sees the same input shape
# regardless of which source the threads came from.
SNIPPET_MAX_CHARS = 200

PHASES = ["Inbox & Triage", "Draft Generation", "Approval Gate", "Export Proof"]

SOURCE_GMAIL = "Gmail (via engine.py)"
SOURCE_SAMPLE = "Sample threads for demo"

# Display buckets, in render order. triage.py can also emit "important",
# "low-priority" and "spam"; those map onto the buckets below so the UI stays
# at the four priorities the workflow cares about.
PRIORITY_BUCKETS = [
    ("urgent", "Urgent", "🚨"),
    ("needs-reply", "Needs Reply", "💬"),
    ("fyi", "FYI", "👀"),
    ("ignore", "Ignore", "🗑️"),
]
PRIORITY_TO_BUCKET = {
    "urgent": "urgent",
    "needs-reply": "needs-reply",
    "important": "fyi",
    "fyi": "fyi",
    "low-priority": "ignore",
    "spam": "ignore",
}
# bucket key -> (label, icon), for priority badges in the draft phase
BUCKET_META = {key: (label, icon) for key, label, icon in PRIORITY_BUCKETS}

# --------------------------------------------------------------------------- #
# Page config & session state
# --------------------------------------------------------------------------- #

st.set_page_config(page_title="The Draft Desk", page_icon="✍️", layout="wide")

# Make sure the .env next to this file is loaded before the lazy engine /
# triage imports run (llm_client.py also loads it on import, but that only
# helps once those modules are actually pulled in).
load_dotenv(WORKSPACE_DIR / ".env")

def _init_session_state() -> None:
    """Initialize all session state at the top of the app.

    One defaults dict, applied with setdefault so a rerun (or a partially
    progressed session) never wipes what the user has already done.
    """
    defaults = {
        "threads": [],                 # pipeline-format threads
        "triaged": [],                 # threads + triage labels
        "drafts": {},                  # thread id -> draft dict
        "approved": {},                # thread id -> approved draft dict
        "rejected": set(),             # thread ids rejected at the gate
        "sent": set(),                 # thread ids sent via the gate's Send button
        "booked": {},                  # thread id -> created calendar event dict
        "gate_celebrated": False,      # Approval Gate balloons fired
        "current_phase": "Inbox & Triage",
        "pipeline_running": False,     # ⚡ full pipeline executing this run
        "pipeline_log": [],            # log strings from the last ⚡ run
        "pipeline_error": None,        # ❌ lines from the last fatal ⚡ run
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


_init_session_state()


# --------------------------------------------------------------------------- #
# Format converters
# --------------------------------------------------------------------------- #

def engine_to_pipeline(raw_threads: list[dict]) -> list[dict]:
    """Convert engine.fetch_threads() output into the pipeline format.

    engine format:  [{"thread_id", "sender", "subject", "snippet", "date"}]
    pipeline:       [{"id", "subject", "messages": [{"from", "date", "body"}]}]

    The snippet becomes the message body for now; a full message fetch can be
    added to engine.py later without touching this UI.
    """
    pipeline: list[dict] = []
    for i, t in enumerate(raw_threads):
        pipeline.append(
            {
                "id": t.get("thread_id") or f"gmail-{i}",
                "subject": t.get("subject") or "(no subject)",
                "messages": [
                    {
                        "from": t.get("sender") or "(unknown sender)",
                        "date": t.get("date") or "",
                        "body": t.get("snippet") or "",
                    }
                ],
            }
        )
    return pipeline


def pipeline_to_triage_format(threads: list[dict]) -> list[dict]:
    """Flatten pipeline threads into the shape triage_inbox() expects.

    triage_inbox() reads "sender", "subject" and "snippet" off each thread and
    merges its labels back onto the dicts it is given, so we pass the "id"
    through as well — that is what lets us map triaged results back to the
    full pipeline threads (with complete message bodies) for display.
    """
    flat: list[dict] = []
    for t in threads:
        messages = t.get("messages") or [{}]
        last = messages[-1]
        flat.append(
            {
                "id": t.get("id", ""),
                "sender": last.get("from", "(unknown sender)"),
                "subject": t.get("subject", "(no subject)"),
                "snippet": (last.get("body") or "")[:SNIPPET_MAX_CHARS],
            }
        )
    return flat


# --------------------------------------------------------------------------- #
# Thread sources
# --------------------------------------------------------------------------- #

def load_sample_threads() -> list[dict]:
    """Load the demo fallback threads from sample_threads.json."""
    if not SAMPLE_THREADS_PATH.exists():
        st.error(f"sample_threads.json not found at `{SAMPLE_THREADS_PATH}`.")
        return []
    try:
        data = json.loads(SAMPLE_THREADS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        st.error(f"Could not read sample_threads.json: {exc}")
        return []
    if not isinstance(data, list) or not data:
        st.error("sample_threads.json is empty or malformed — expected a non-empty list of threads.")
        return []

    cleaned: list[dict] = []
    for i, thread in enumerate(data):
        if not isinstance(thread, dict) or "messages" not in thread:
            st.warning(f"Sample thread #{i} is malformed — expected 'subject' + 'messages'. Skipped.")
            continue
        thread = dict(thread)
        thread.setdefault("id", f"sample-{i}")
        thread.setdefault("subject", "(no subject)")
        cleaned.append(thread)

    if not cleaned:
        st.error("No usable threads found in sample_threads.json.")
        return []
    st.toast(f"Loaded {len(cleaned)} sample threads.", icon="📄")
    return cleaned


def pull_and_triage(source: str) -> None:
    """Populate session state from the selected source, then triage it.

    engine.py and triage.py are imported lazily: engine.py drags in the
    whole Gmail MCP stack, and triage needs a configured LLM key (OpenRouter
    or Gemini) at call time. Importing eagerly would crash the whole app on
    machines without keys. This way the Desk still opens, and the error
    surfaces where the user can read it.
    """
    # 1) Pull the threads.
    if source == SOURCE_GMAIL:
        try:
            from engine import fetch_threads  # pulls in triage -> LLM key needed

            with st.status("Pulling threads from Gmail via engine.py…", expanded=True) as status:
                st.write("Launching the Gmail MCP client and fetching inbox threads…")
                raw = fetch_threads()
                st.write(f"Received {len(raw)} thread(s) from the Gmail MCP server.")
                st.write("Converting to pipeline format (snippet becomes the body for now)…")
                st.session_state.threads = engine_to_pipeline(raw)
                status.update(
                    label=f"Pulled {len(st.session_state.threads)} thread(s) from Gmail",
                    state="complete",
                    expanded=False,
                )
        except Exception as exc:  # noqa: BLE001 — surface any MCP/auth failure in the UI
            st.error(f"Gmail pull failed: {exc}")
            st.info("Tip: switch the source to **Sample threads for demo** to run the pipeline without Gmail.", icon="💡")
            return
    else:
        st.session_state.threads = load_sample_threads()
        if not st.session_state.threads:
            st.session_state.triaged = []
            return

    # 2) Classify them.
    flat = pipeline_to_triage_format(st.session_state.threads)
    try:
        from triage import triage_inbox

        with st.spinner(f"Classifying {len(flat)} thread(s) with triage.py (LLM backend)…"):
            st.session_state.triaged = triage_inbox(flat)
        st.success(f"Triaged {len(st.session_state.triaged)} thread(s).")
    except Exception as exc:  # noqa: BLE001 — bad key, quota, network, parse errors…
        st.session_state.triaged = []
        st.error(f"Triage failed: {exc}")
        st.caption("The threads were pulled and are still shown below as **Untriaged**.")


def fetch_threads_via_engine() -> list[dict]:
    """Fetch live inbox threads via engine.py and convert to pipeline format.

    Headless fetch for the ⚡ full pipeline (mirrors the Gmail branch of
    pull_and_triage()) — but raises instead of rendering st.error, so the
    caller decides how failures are reported.
    """
    from engine import fetch_threads  # lazy: pulls in the Gmail MCP stack

    return engine_to_pipeline(fetch_threads())


def triage_threads(threads: list[dict]) -> list[dict]:
    """Classify pipeline threads with triage_inbox() (LLM backend).

    Headless twin of pull_and_triage()'s classify step: flattens to the shape
    triage.py expects and returns the triaged list (priority, category, …
    merged onto each thread). Raises instead of rendering UI on failure.
    """
    from triage import triage_inbox  # lazy: pulls in llm_client

    return triage_inbox(pipeline_to_triage_format(threads))


@st.cache_resource
def _get_draft_reply():
    """Lazily import draft_machine.draft_reply and cache the pair.

    Same convention as _get_send_reply()/the calendar trio: draft_machine
    drags in the whole LLM client chain, so the import stays lazy and any
    missing-package failure surfaces at the drafting step, not app start.

    Returns (draft_reply, MODEL).
    """
    from draft_machine import MODEL, draft_reply

    return draft_reply, MODEL


def run_full_pipeline() -> list[str]:
    """Fetch → triage → draft with ZERO UI; returns the run as log strings.

    The headless twin of _render_pipeline_execution(): identical steps and
    session-state effects, but every outcome becomes a log line instead of an
    st.write/st.status call, so tests can execute the pipeline without
    rendering anything.

    Fatal fetch/triage failures log a ❌ line and stop the run early (the
    Approval Gate phase is never set); one failed draft is logged and the
    loop continues. The phase moves to "Approval Gate" only when the run
    reaches the end of the draft loop.
    """
    log: list[str] = []
    source = st.session_state.get("source", SOURCE_SAMPLE)

    # 1) Fetch from the selected source.
    try:
        if source == SOURCE_GMAIL:
            st.session_state.threads = fetch_threads_via_engine()
            where = "Gmail (engine.py)"
        else:
            st.session_state.threads = load_sample_threads()
            where = "sample_threads.json"
        n = len(st.session_state.threads)
        if n == 0:
            raise RuntimeError("no usable threads came back from the source")
        log.append(f"Fetched {n} thread(s) from {where}.")
    except Exception as exc:  # noqa: BLE001 — MCP/auth/missing-file failures
        log.append(f"❌ Fetch failed: {exc}")
        return log

    # 2) Classify.
    try:
        st.session_state.triaged = triage_threads(st.session_state.threads)
    except Exception as exc:  # noqa: BLE001 — bad key, quota, network, parse errors…
        log.append(f"❌ Triage failed: {exc}")
        return log
    triaged = st.session_state.triaged
    n_urgent = sum(1 for t in triaged if t.get("priority") == "urgent")
    n_reply = sum(1 for t in triaged if t.get("priority") == "needs-reply")
    log.append(f"Triaged {len(triaged)} thread(s): {n_urgent} urgent, {n_reply} needs-reply.")

    # 3) Reset downstream state — a fresh run starts with a clean gate.
    st.session_state.drafts = {}
    st.session_state.approved = {}
    st.session_state.rejected = set()
    st.session_state.sent = set()
    st.session_state.booked = {}

    # 4) Draft every urgent + needs-reply thread; one failure never stops it.
    actionable = [t for t in triaged if t.get("priority") in ("urgent", "needs-reply")]
    total = len(actionable)
    drafted = 0
    if total:
        threads_by_id = {t.get("id"): t for t in st.session_state.threads}
        try:
            draft_reply, model = _get_draft_reply()
        except Exception as exc:  # noqa: BLE001 — draft_machine import failure
            log.append(f"❌ Could not load draft_machine.py: {exc}")
            return log
        from llm_client import configured_backends

        if not configured_backends():
            log.append(
                "❌ No LLM API key is set — add OPENROUTER_API_KEY or GEMINI_API_KEY "
                "to the .env file; drafting cannot run."
            )
            return log

        for i, item in enumerate(actionable, start=1):
            subject = item.get("subject", "(no subject)")
            try:
                thread = _thread_for_drafting(item, threads_by_id)
                draft = draft_reply(thread)
                last = (thread.get("messages") or [{}])[-1]
                st.session_state.drafts[item.get("id")] = {
                    "draft": draft,
                    "model": model,
                    "subject": subject,
                    "reply_to": last.get("from", "(unknown sender)"),
                }
                # Fresh draft = unreviewed: drop stale verdicts + editor state.
                st.session_state.approved.pop(item.get("id"), None)
                st.session_state.rejected.discard(item.get("id"))
                st.session_state.sent.discard(item.get("id"))
                st.session_state.pop(f"draft_text_{item.get('id')}", None)
                drafted += 1
                log.append(f"Draft {i}/{total}: {subject} — done")
            except Exception as exc:  # noqa: BLE001 — one bad draft, keep going
                log.append(f'❌ Draft {i}/{total} failed for "{subject}": {exc} — continuing')
    else:
        log.append("No urgent or needs-reply threads — nothing to draft.")

    # 5) Wrap-up: move to the gate and report.
    st.session_state.current_phase = "Approval Gate"
    if drafted:
        st.session_state.gate_celebrated = False  # re-arm the gate balloons
    log.append(f"Pipeline complete! {drafted} draft(s) ready for review.")
    return log


def _render_pipeline_execution() -> None:
    """⚡ Execute the full pipeline inside a live st.status container.

    The UI twin of run_full_pipeline() — and deliberately NOT its caller: the
    steps run inline so the container's label can update at each step boundary
    and st.write each outcome (✅ / ❌) as it lands.

    Failure model:
      - fetch/triage failures are FATAL: the ❌ is written, the container flips
        to state="error", later steps are skipped, and the wrap-up below still
        runs. The steps are guarded by `fatal` instead of a bare `return`
        because a return inside the `with` block would skip the rerun and
        leave pipeline_running dangling True.
      - per-draft failures are ❌-written and the loop continues.

    The wrap-up (pipeline_log, phase, pipeline_running, st.rerun) runs OUTSIDE
    the status block on every path — the final st.rerun() is outside too.
    """
    log: list[str] = []
    fatal = False
    drafted = 0
    st.session_state.pipeline_error = None  # a new run clears the old failure

    with st.status("Running full pipeline...", expanded=True) as status:
        source = st.session_state.get("source", SOURCE_SAMPLE)

        # --- Step 1/3 — fetch ------------------------------------------------ #
        status.update(label="Step 1/3 — fetching threads…")
        try:
            if source == SOURCE_GMAIL:
                st.session_state.threads = fetch_threads_via_engine()
                where = "Gmail (engine.py)"
            else:
                st.session_state.threads = load_sample_threads()
                where = "sample_threads.json"
            n = len(st.session_state.threads)
            if n == 0:
                raise RuntimeError("no usable threads came back from the source")
            st.write(f"✅ Fetched {n} thread(s) from {where}.")
            log.append(f"Fetched {n} thread(s) from {where}.")
        except Exception as exc:  # noqa: BLE001 — MCP/auth/missing-file failures
            st.write(f"❌ Fetch failed: {exc}")
            log.append(f"❌ Fetch failed: {exc}")
            fatal = True

        # --- Step 2/3 — triage ----------------------------------------------- #
        if not fatal:
            status.update(label="Step 2/3 — classifying threads (LLM)…")
            try:
                st.session_state.triaged = triage_threads(st.session_state.threads)
                triaged = st.session_state.triaged
                n_urgent = sum(1 for t in triaged if t.get("priority") == "urgent")
                n_reply = sum(1 for t in triaged if t.get("priority") == "needs-reply")
                st.write(
                    f"✅ Triaged {len(triaged)} thread(s): {n_urgent} urgent, "
                    f"{n_reply} needs-reply."
                )
                log.append(
                    f"Triaged {len(triaged)} thread(s): {n_urgent} urgent, "
                    f"{n_reply} needs-reply."
                )
            except Exception as exc:  # noqa: BLE001 — bad key, quota, network…
                st.write(f"❌ Triage failed: {exc}")
                log.append(f"❌ Triage failed: {exc}")
                fatal = True

        # --- Step 3/3 — draft loop ------------------------------------------- #
        if not fatal:
            status.update(label="Step 3/3 — drafting replies (urgent + needs-reply)…")
            # Fresh run = clean gate (same reset as run_full_pipeline()).
            st.session_state.drafts = {}
            st.session_state.approved = {}
            st.session_state.rejected = set()
            st.session_state.sent = set()
            st.session_state.booked = {}
            try:
                draft_reply, model = _get_draft_reply()
                from llm_client import configured_backends

                if not configured_backends():
                    raise RuntimeError(
                        "no LLM API key is set — add OPENROUTER_API_KEY or "
                        "GEMINI_API_KEY to the .env file"
                    )
                actionable = [
                    t for t in st.session_state.triaged
                    if t.get("priority") in ("urgent", "needs-reply")
                ]
                total = len(actionable)
                threads_by_id = {t.get("id"): t for t in st.session_state.threads}
                if total == 0:
                    st.write("✅ No urgent or needs-reply threads — nothing to draft.")
                for i, item in enumerate(actionable, start=1):
                    subject = item.get("subject", "(no subject)")
                    try:  # per-draft: ❌ + continue, never stop the run
                        thread = _thread_for_drafting(item, threads_by_id)
                        draft = draft_reply(thread)
                        last = (thread.get("messages") or [{}])[-1]
                        st.session_state.drafts[item.get("id")] = {
                            "draft": draft,
                            "model": model,
                            "subject": subject,
                            "reply_to": last.get("from", "(unknown sender)"),
                        }
                        st.session_state.approved.pop(item.get("id"), None)
                        st.session_state.rejected.discard(item.get("id"))
                        st.session_state.sent.discard(item.get("id"))
                        st.session_state.pop(f"draft_text_{item.get('id')}", None)
                        drafted += 1
                        st.write(f"✅ Draft {i}/{total}: {subject} — done")
                        log.append(f"Draft {i}/{total}: {subject} — done")
                    except Exception as exc:  # noqa: BLE001 — one bad draft, keep going
                        st.write(f'❌ Draft {i}/{total} failed: "{subject}" — {exc}')
                        log.append(
                            f'❌ Draft {i}/{total} failed for "{subject}": {exc} — continuing'
                        )
            except Exception as exc:  # noqa: BLE001 — loader/key failures (fatal for the step)
                st.write(f"❌ Draft step failed: {exc}")
                log.append(f"❌ Draft step failed: {exc}")
                fatal = True

        # --- Final state of the container ------------------------------------- #
        if fatal:
            status.update(label="Pipeline stopped — see the ❌ step above.", state="error")
        else:
            status.update(
                label=f"Pipeline complete — {drafted} draft(s) ready for review.",
                state="complete",
                expanded=False,
            )
            log.append(f"Pipeline complete! {drafted} draft(s) ready for review.")

    # --- OUTSIDE the status block — runs on every path, then one rerun ------ #
    st.session_state.pipeline_log = log
    # Fatal runs land back on the Inbox, where the ❌ lives only in the
    # (now-gone) status container — persist it so main() can explain why the
    # threads show as Untriaged instead of a silent dead end.
    st.session_state.pipeline_error = (
        "\n".join(entry for entry in log if "❌" in entry) if fatal else None
    )
    if drafted:
        st.session_state.gate_celebrated = False  # re-arm the gate balloons
    if not fatal:
        st.session_state.current_phase = "Approval Gate"
    st.session_state.pipeline_running = False
    st.rerun()


@st.cache_resource
def _get_send_reply():
    """Lazily import engine.send_reply and cache the resolved callable.

    The Desk's one shortcut to the outside world: engine.py drags in the whole
    Gmail MCP stack (and send_reply additionally needs the raw Gmail API
    client), so the import must stay lazy and any failure must surface at the
    Send button instead of at app start. cache_resource keeps repeat Send
    clicks from re-importing.
    """
    from engine import send_reply

    return send_reply


@st.cache_resource
def _get_calendar_engine():
    """Lazily import calendar_engine's meeting tools and cache the trio.

    Mirrors _get_send_reply(): calendar_engine.py shares engine.py's OAuth
    grant and pulls in the raw Google Calendar API client, so the import
    must stay lazy and any failure must surface at the Book Meeting button
    instead of at app start. cache_resource keeps repeat Book clicks from
    re-importing.

    Returns (parse_meeting_request, find_free_slot, create_event,
    last_availability_error) — the last one explains why a check said
    "busy"/None, since both checks fail closed on any calendar problem.
    """
    from calendar_engine import (
        create_event,
        find_free_slot,
        last_availability_error,
        parse_meeting_request,
    )

    return (
        parse_meeting_request,
        find_free_slot,
        create_event,
        last_availability_error,
    )


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #

def _bucket_icon(priority: str | None) -> str:
    """Emoji for a triage priority, via its display bucket."""
    return BUCKET_META.get(PRIORITY_TO_BUCKET.get(priority), (None, "❔"))[1]


def render_thread_card(item: dict, thread: dict | None) -> None:
    """One expandable thread: triage metadata on top, full messages below."""
    sender = item.get("sender") or "(unknown sender)"
    subject = item.get("subject") or "(no subject)"
    with st.expander(f"{subject} — {sender}"):
        st.markdown(
            f"**Priority:** `{item.get('priority', '—')}` | "
            f"**Category:** `{item.get('category', '—')}` | "
            f"**Recommended action:** `{item.get('action', '—')}` | "
            f"**Needs reply:** `{item.get('needs_reply', '—')}` | "
            f"**Deadline:** `{item.get('deadline', 'none')}` | "
            f"**Confidence:** `{item.get('confidence', '—')}`"
        )
        if item.get("reason"):
            st.markdown(f"🧠 **Triage reason:** {item['reason']}")
        if item.get("why_it_matters"):
            st.markdown(f"⭐ **Why it matters:** {item['why_it_matters']}")

        st.divider()
        messages = (thread or {}).get("messages") or []
        if not messages and item.get("snippet"):
            # No pipeline thread matched (or none was stored) — fall back to
            # the snippet that triage actually saw.
            messages = [{"from": sender, "date": "", "body": item["snippet"]}]
        for i, msg in enumerate(messages):
            st.markdown(f"**✉️ {msg.get('from', '(unknown sender)')}**")
            if msg.get("date"):
                st.caption(msg["date"])
            st.markdown((msg.get("body") or "_(empty body)_").strip())
            if i < len(messages) - 1:
                st.divider()


def render_thread_groups() -> None:
    """Render triaged threads grouped by priority bucket (or untriaged)."""
    threads_by_id = {t.get("id"): t for t in st.session_state.threads}

    if st.session_state.triaged:
        buckets: dict[str, list[dict]] = {key: [] for key, _, _ in PRIORITY_BUCKETS}
        buckets["untriaged"] = []
        for item in st.session_state.triaged:
            bucket = PRIORITY_TO_BUCKET.get(item.get("priority"))
            buckets[bucket if bucket else "untriaged"].append(item)

        for key, label, icon in PRIORITY_BUCKETS + [("untriaged", "Untriaged", "❔")]:
            items = buckets[key]
            if not items:
                continue
            st.subheader(f"{icon} {label} ({len(items)})")
            if key == "untriaged":
                st.caption(
                    "These couldn't be classified — usually the LLM quota ran "
                    "out (or the reply format couldn't be parsed). Re-run "
                    "**⚡ Run Full Pipeline** once the quota resets; the "
                    "⚠️ banner at the top has details."
                )
            for item in items:
                render_thread_card(item, threads_by_id.get(item.get("id")))
    elif st.session_state.threads:
        # Threads were pulled but triage never ran or failed — still show them.
        st.subheader(f"❔ Untriaged ({len(st.session_state.threads)})")
        st.caption(
            "Triage didn't complete for these threads — usually the LLM "
            "quota is exhausted. Press **⚡ Run Full Pipeline** (sidebar) once "
            "the quota resets; the ⚠️ banner at the top has details."
        )
        for t in st.session_state.threads:
            last = (t.get("messages") or [{}])[-1]
            render_thread_card(
                {
                    "id": t.get("id"),
                    "subject": t.get("subject", "(no subject)"),
                    "sender": last.get("from", "(unknown sender)"),
                    "snippet": (last.get("body") or "")[:SNIPPET_MAX_CHARS],
                    "priority": "untriaged",
                },
                t,
            )


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #

def render_inbox_phase() -> None:
    """Phase 1 — pull threads and classify them by priority."""
    st.header("📥 Inbox & Triage")
    st.caption(
        f"Phase {PHASES.index('Inbox & Triage') + 1} of {len(PHASES)} — "
        f"Pull threads from the selected source, then triage them by priority. Once triaged, the highest-priority threads move to Draft Generation."
    )

    if st.button(
        "Pull & Triage Threads",
        key="pull_and_triage_btn",
        type="primary",
        width="stretch",
    ):
        pull_and_triage(st.session_state.source)

    st.divider()

    triaged = st.session_state.triaged
    if triaged:
        def _count(bucket: str) -> int:
            return sum(
                1 for item in triaged if PRIORITY_TO_BUCKET.get(item.get("priority")) == bucket
            )

        n_urgent = _count("urgent")
        n_reply = _count("needs-reply")
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Total threads", len(st.session_state.threads))
        c2.metric("🚨 Urgent", n_urgent)
        c3.metric("💬 Needs reply", n_reply)
        c4.metric("⚡ Actionable", n_urgent + n_reply, help="urgent + needs-reply")
        c5.metric("👀 FYI", _count("fyi"))
        c6.metric("🗑️ Ignore", _count("ignore"))
        st.divider()
    elif not st.session_state.threads:
        st.info(
            "No threads loaded yet. Pick a source in the sidebar, then click "
            "**Pull & Triage Threads**.",
            icon="📮",
        )

    render_thread_groups()


def _thread_for_drafting(item: dict, threads_by_id: dict) -> dict:
    """Best pipeline thread for a triaged item, with a snippet-based fallback.

    draft_machine/context_builder expect the pipeline format (subject +
    messages). The full thread normally sits in st.session_state.threads; if
    it ever goes missing, rebuild a minimal one from what triage saw.
    """
    thread = threads_by_id.get(item.get("id"))
    if thread and thread.get("messages"):
        return thread
    return {
        "id": item.get("id"),
        "subject": item.get("subject", "(no subject)"),
        "messages": [
            {
                "from": item.get("sender", "(unknown sender)"),
                "date": "",
                "body": item.get("snippet", ""),
            }
        ],
    }


def generate_all_drafts(actionable: list[dict], threads_by_id: dict) -> None:
    """Draft a reply for every actionable thread via draft_machine.draft_reply().

    draft_machine (through context_builder) loads tone_profile.json and
    past_replies.json, builds the persona + few-shot prompt, and calls the
    LLM backend chain (OpenRouter primary, Gemini fallback). Each draft is
    stored in st.session_state.drafts[thread_id] as a dict holding the reply
    text plus the metadata the Approval Gate needs (model, subject, reply_to).
    """
    from draft_machine import MODEL, draft_reply  # lazy: pulls in llm_client
    from llm_client import configured_backends

    if not configured_backends():
        st.error("No LLM API key is set — draft_machine.py cannot draft.")
        st.caption(
            "Fix: add `OPENROUTER_API_KEY` (primary) or `GEMINI_API_KEY` (fallback) "
            "to the `.env` file next to llm_client.py, then reload the app."
        )
        return

    total = len(actionable)
    progress = st.progress(0.0, text=f"Drafting 0/{total}…")
    drafted = 0

    for i, item in enumerate(actionable):
        subject = item.get("subject", "(no subject)")
        progress.progress(i / total, text=f"Drafting {i + 1}/{total}: {subject}")

        thread = _thread_for_drafting(item, threads_by_id)
        try:
            draft = draft_reply(thread)
            last = (thread.get("messages") or [{}])[-1]
            st.session_state.drafts[item.get("id")] = {
                "draft": draft,
                "model": MODEL,
                "subject": subject,
                "reply_to": last.get("from", "(unknown sender)"),
            }
            # Phase 3 coherence: a freshly generated draft is unreviewed, so
            # drop any stale verdict plus the gate's saved editor state (those
            # widgets are not instantiated in this run) — otherwise a drafted-
            # then-decided thread could never return to the pending queue.
            st.session_state.approved.pop(item.get("id"), None)
            st.session_state.rejected.discard(item.get("id"))
            st.session_state.sent.discard(item.get("id"))
            st.session_state.pop(f"draft_text_{item.get('id')}", None)
            drafted += 1
        except Exception as exc:  # noqa: BLE001 — bad key, quota, network, parse errors…
            st.error(f'Draft failed for "{subject}": {exc}')

    progress.progress(
        1.0, text=f"Drafting complete — {drafted}/{total} draft(s) generated."
    )
    if drafted:
        st.toast(f"Drafted {drafted} of {total} actionable thread(s).", icon="🖊️")
        st.session_state.gate_celebrated = False  # re-arm the Approval Gate balloons


def render_draft_pair(item: dict, thread: dict | None) -> None:
    """One actionable thread + its AI draft, side by side.

    Left: the original thread (latest message). Right: the draft that
    draft_machine produced for it, from st.session_state.drafts.
    """
    record = st.session_state.drafts.get(item.get("id")) or {}
    draft_text = (record.get("draft") or "").strip()

    sender = item.get("sender", "(unknown sender)")
    subject = item.get("subject", "(no subject)")
    icon = _bucket_icon(item.get("priority"))

    # Latest message only — the full history stays viewable in Inbox & Triage.
    messages = (thread or {}).get("messages") or []
    if not messages and item.get("snippet"):
        messages = [{"from": sender, "date": "", "body": item["snippet"]}]
    last = messages[-1] if messages else {}

    left, right = st.columns(2)
    with left:
        with st.expander(f"{icon} {subject} — {sender}"):
            st.markdown(
                f"**Priority:** `{item.get('priority', '—')}` | "
                f"**Recommended action:** `{item.get('action', '—')}`"
            )
            st.divider()
            st.markdown(f"**✉️ {last.get('from', '(unknown sender)')}**")
            if last.get("date"):
                st.caption(last["date"])
            st.markdown((last.get("body") or "_(empty body)_").strip())
            if len(messages) > 1:
                st.caption(
                    f"+ {len(messages) - 1} earlier message(s) — "
                    "full history in **Inbox & Triage**."
                )

    with right:
        with st.expander(f"🖊️ AI draft — {record.get('model', 'LLM')}", expanded=True):
            if record.get("reply_to"):
                st.caption(f"Replying to **{record['reply_to']}**")
            st.markdown(draft_text or "_(empty draft)_")


def render_draft_phase() -> None:
    """Phase 2 — draft replies for every actionable (urgent + needs-reply) thread."""
    st.header("🖊️ Draft Generation")
    st.caption(
        f"Phase {PHASES.index('Draft Generation') + 1} of {len(PHASES)} — draft replies "
        "with draft_machine.py (tone profile + past-reply examples via context_builder)."
    )

    actionable = [
        item
        for item in st.session_state.triaged
        if PRIORITY_TO_BUCKET.get(item.get("priority")) in ("urgent", "needs-reply")
    ]
    if not actionable:
        st.warning("No actionable threads yet — run **Inbox & Triage** first.")
        return

    threads_by_id = {t.get("id"): t for t in st.session_state.threads}
    st.info(
        f"**{len(actionable)}** actionable thread(s) in the queue "
        "(🚨 urgent + 💬 needs-reply). Generating re-drafts all of them — "
        "existing drafts are replaced.",
        icon="✍️",
    )
    if st.button(
        "Generate All Drafts",
        key="generate_all_drafts_btn",
        type="primary",
        width="stretch",
    ):
        generate_all_drafts(actionable, threads_by_id)

    st.divider()

    drafted = [item for item in actionable if item.get("id") in st.session_state.drafts]
    if not drafted:
        st.info(
            "Click **Generate All Drafts** to draft a reply for every thread in the queue.",
            icon="🖊️",
        )
        return

    for item in drafted:
        render_draft_pair(item, threads_by_id.get(item.get("id")))

    missing = len(actionable) - len(drafted)
    if missing:
        st.warning(
            f"{missing} actionable thread(s) still have no draft — "
            "click **Generate All Drafts** to retry.",
            icon="⚠️",
        )
    else:
        st.success(
            f"All **{len(drafted)}** draft(s) ready for review. Head to the "
            "**✅ Approval Gate** (next phase, sidebar) to approve, edit, or "
            "reject each one.",
            icon="🚦",
        )


def _approve_callback(tid: str) -> None:
    """on_click for Approve: snapshot the (possibly edited) text into approved.

    The text box's live value is what gets saved — edits made before clicking
    are part of the approved record. on_click callbacks run pre-rerun, so
    cleaning up the editor's widget state here is safe.
    """
    draft_record = st.session_state.drafts.get(tid) or {}
    generated = draft_record.get("draft", "")
    box_value = st.session_state.get(f"draft_text_{tid}")
    final_text = str(generated if box_value is None else box_value).strip()
    item = next((i for i in st.session_state.triaged if i.get("id") == tid), {})
    # Same record shape approval_gate.py logs to approved_drafts.json, plus the
    # thread id + priority so Export Proof can join back to the triage queue.
    st.session_state.approved[tid] = {
        "approved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "ready_to_send",
        "edited": final_text != generated.strip(),
        "id": tid,
        "thread_subject": draft_record.get("subject", "(no subject)"),
        "reply_to": draft_record.get("reply_to", "(unknown sender)"),
        "model": draft_record.get("model", ""),
        "draft": final_text,
        "priority": (item or {}).get("priority", "—"),
    }
    st.session_state.pop(f"draft_text_{tid}", None)
    st.rerun()


def _regenerate_callback(tid: str) -> None:
    """on_click for Regenerate: re-run draft_reply for this thread and swap the
    fresh text into the editor. An LLM failure is surfaced inside the card via
    regen_error_<tid> instead of crashing the callback."""
    record = st.session_state.drafts.get(tid) or {}
    item = next((i for i in st.session_state.triaged if i.get("id") == tid), None)
    if item is None:
        item = {
            "id": tid,
            "subject": record.get("subject", "(no subject)"),
            "sender": record.get("reply_to", "(unknown sender)"),
            "snippet": "",
        }
    try:
        from draft_machine import draft_reply  # lazy: pulls in llm_client

        threads_by_id = {t.get("id"): t for t in st.session_state.threads}
        fresh = draft_reply(_thread_for_drafting(item, threads_by_id))
        record["draft"] = fresh
        st.session_state.drafts[tid] = record
        st.session_state[f"draft_text_{tid}"] = fresh  # pre-rerun: legal here
        st.session_state.pop(f"regen_error_{tid}", None)
    except Exception as exc:  # noqa: BLE001 — bad key, quota, network, parse errors…
        st.session_state[f"regen_error_{tid}"] = str(exc)
    st.rerun()


def _reject_callback(tid: str) -> None:
    """on_click for Reject: mark the thread rejected at the gate."""
    st.session_state.rejected.add(tid)
    st.session_state.pop(f"draft_text_{tid}", None)
    st.rerun()


# Pulls the addr-spec out of headers like "Priya Sharma <p@acme.com>"
# (same pattern mailer.py uses).
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _extract_email_address(from_header: str) -> str:
    """Return the bare address from a From header, e.g.
    'Priya Sharma <priya.sharma@acme.com>' -> 'priya.sharma@acme.com'."""
    match = _EMAIL_RE.search(from_header or "")
    return match.group(0) if match else ""


def _send_callback(tid: str) -> None:
    """on_click for Send: email the approved draft via engine.send_reply().

    The one gate action that reaches the outside world — the reply really
    leaves the user's Gmail account — so it requires an approved draft and
    reports both outcomes through session state:
      success -> the approved record flips to status "sent" (sent_at,
                 sent_to, gmail_message_id) and the thread id joins
                 st.session_state.sent, which drives the 📨 Sent badge;
      failure -> send_error_<tid> holds the message the card surfaces.
    """
    record = st.session_state.approved.get(tid) or {}
    item = next((i for i in st.session_state.triaged if i.get("id") == tid), None)
    if item is None:
        item = {
            "id": tid,
            "subject": record.get("thread_subject", "(no subject)"),
            "sender": record.get("reply_to", "(unknown sender)"),
            "snippet": "",
        }
    threads_by_id = {t.get("id"): t for t in st.session_state.threads}
    messages = _thread_for_drafting(item, threads_by_id).get("messages") or []

    # Recipient = the sender of the thread's LAST message — who we reply to.
    # Handles both bare addresses and "Name <address>" headers.
    last_from = messages[-1].get("from", "") if messages else ""
    recipient = _extract_email_address(last_from)
    body = (record.get("draft") or "").strip()

    error = ""
    if not recipient:
        error = (
            "Could not extract a recipient address from the last message "
            f"({last_from or '(unknown sender)'}) — cannot send."
        )
    elif not body:
        error = "The approved draft is empty — nothing to send."
    else:
        try:
            send_reply = _get_send_reply()
            result = send_reply(
                thread_id=tid,
                to=recipient,
                subject=record.get("thread_subject", "(no subject)"),
                body=body,
            )
        except Exception as exc:  # noqa: BLE001 — auth, quota, network, API errors…
            error = str(exc)

    if error:
        st.session_state[f"send_error_{tid}"] = error
    else:
        # Success: flip the record to "sent" and track the thread id.
        record["status"] = "sent"
        record["sent_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        record["sent_to"] = recipient
        record["gmail_message_id"] = result.get("message_id", "")
        st.session_state.approved[tid] = record
        st.session_state.sent.add(tid)
        st.session_state.pop(f"send_error_{tid}", None)
        # Audit trail: the send really left the account — record it. The id
        # rides under "message_id" (engine.send_reply's shape); "id" kept for
        # any caller that returns the raw Gmail response.
        message_id = result.get("id") or result.get("message_id", "")
        if message_id:
            log_action(
                action_type="sent",
                thread_subject=record.get("thread_subject", "(no subject)"),
                detail=recipient,
                action_id=message_id,
            )
    st.rerun()


def _render_booked_state(booked: dict) -> None:
    """Booked confirmation for a meeting-request record: the calendar link
    that replaces the 📅 Book Meeting button once the slot is taken."""
    booked = booked or {}
    when = (booked.get("start") or {}).get("dateTime", "")
    when_text = f" for **{when}**" if when else ""
    link = booked.get("htmlLink") or ""
    if link:
        st.success(f"📅 Booked{when_text} — [Open in Google Calendar]({link})", icon="📅")
    else:
        st.success(f"📅 Booked{when_text} on your primary calendar.", icon="📅")


def _book_meeting_flow(tid: str, item: dict) -> None:
    """Body of the 📅 Book Meeting click: parse -> check -> book, with a
    progress spinner and its own report at every step.

    Runs in the script body right after the click (not as an on_click
    callback) so the st.spinner() progress indicators actually render. The
    three steps come from calendar_engine.py:

      1. parse_meeting_request(thread) — Gemini pulls the topic, attendees,
         duration and proposed times out of the thread; a parsing_error
         aborts with st.error before anything is checked or booked.
      2. find_free_slot(proposed, duration) — the first proposed time that
         is free on the primary calendar; none free aborts with st.warning,
         leaving the button in place for a retry.
      3. create_event(...) — inserts the event and emails invitations
         (sendUpdates="all"); any API failure aborts with st.error and
         nothing is stored.

    On success the created event dict is stored in
    st.session_state.booked[tid] and the rerun swaps the Book button for
    the calendar link.
    """
    threads_by_id = {t.get("id"): t for t in st.session_state.threads}
    thread = _thread_for_drafting(item, threads_by_id)

    try:
        (
            parse_meeting_request,
            find_free_slot,
            create_event,
            last_availability_error,
        ) = _get_calendar_engine()

        # 1) Extract the meeting details with Gemini.
        with st.spinner("🧠 Parsing the meeting request (Gemini)…"):
            details = parse_meeting_request(thread)
        if (details or {}).get("parsing_error"):
            st.error(
                "📅 Booking stopped — could not parse the meeting request: "
                f"{details['parsing_error']}"
            )
            return

        topic = details.get("topic") or "(untitled meeting)"
        attendees = details.get("attendees") or []
        duration = details.get("duration_minutes")
        proposed = details.get("proposed_times") or []
        st.info(
            f"**📅 {topic}**\n\n"
            f"- **Attendees:** {', '.join(f'`{a}`' for a in attendees) or '_(none found)_'}\n"
            f"- **Duration:** {duration} min\n"
            f"- **Proposed times:** "
            f"{' · '.join(f'`{t}`' for t in proposed) or '_(none found)_'}",
            icon="🧠",
        )

        # 2) Verify the proposed times against the primary calendar.
        with st.spinner("🔍 Checking the proposed times against your calendar…"):
            slot = find_free_slot(proposed, duration)
        if not slot:
            reason = last_availability_error()
            message = (
                "📅 None of the proposed times are free (or none were usable) — "
                "nothing was booked. Reply to suggest new times instead."
            )
            if reason:
                # find_free_slot fails closed, so say WHY it found nothing —
                # a broken check (disabled Calendar API, auth, quota) must not
                # masquerade as a full calendar.
                message += f"\n\n**Why:** {reason}"
            st.warning(message, icon="🚫")
            return

        # 3) Insert the event — sendUpdates="all" invites every attendee.
        with st.spinner(f"📆 Creating the event at {slot} and inviting attendees…"):
            event = create_event(
                summary=topic,
                start_time=slot,
                duration_minutes=duration,
                attendees=attendees,
                description=(
                    "Booked by The Draft Desk from email thread "
                    f"`{tid}` — original subject: "
                    f"{(item or {}).get('subject', '(no subject)')}"
                ),
            )
    except Exception as exc:  # noqa: BLE001 — import/auth/quota/network/API errors
        st.error(f"📅 Booking failed: {exc}")
        return

    st.session_state.booked[tid] = event
    # Audit trail: the event really exists on the calendar — record it.
    event_id = (event or {}).get("id", "")
    if event_id:
        log_action(
            action_type="booked",
            thread_subject=(item or {}).get("subject", "(no subject)"),
            detail=details.get("topic") or (item or {}).get("subject", "(no subject)"),
            action_id=event_id,
        )
    link = (event or {}).get("htmlLink") or ""
    confirmation = f"📅 **Booked {slot}**"
    if link:
        confirmation += f" — [Open in Google Calendar]({link})"
    st.success(confirmation + " — attendees have been invited.", icon="📅")
    st.rerun()


def _render_review_card(tid: str, record: dict, item: dict | None) -> None:
    """One pending draft: full original thread left, editable draft right,
    Approve / Regenerate / Reject buttons below."""
    item = item or {}
    subject = record.get("subject", "(no subject)")
    icon = _bucket_icon(item.get("priority"))

    st.markdown(f"### {icon} {subject}")

    regen_error = st.session_state.pop(f"regen_error_{tid}", None)
    if regen_error:
        st.error(f"🔄 Regenerate failed for this draft: {regen_error}")

    threads_by_id = {t.get("id"): t for t in st.session_state.threads}
    messages = _thread_for_drafting(item, threads_by_id).get("messages") or []

    left, right = st.columns(2)
    with left:
        if item.get("priority"):
            st.markdown(
                f"**Priority:** `{item.get('priority', '—')}` | "
                f"**Category:** `{item.get('category', '—')}` | "
                f"**Recommended action:** `{item.get('action', '—')}`"
            )
            if item.get("category") == "meeting-request":
                st.info(
                    "📅 This thread is a **meeting request** — after you press "
                    "**✅ Approve**, a **📅 Book Meeting** button appears next to "
                    "📨 Send reply in the *🗂️ Reviewed decisions* card below.",
                    icon="📅",
                )
            st.divider()
        for i, msg in enumerate(messages):
            st.markdown(f"**✉️ {msg.get('from', '(unknown sender)')}**")
            if msg.get("date"):
                st.caption(msg["date"])
            st.markdown((msg.get("body") or "_(empty body)_").strip())
            if i < len(messages) - 1:
                st.divider()
    with right:
        st.caption("Editable draft — this exact text is what Approve saves")
        st.text_area(
            "Draft",
            value=record.get("draft", ""),
            key=f"draft_text_{tid}",
            height=260,
            label_visibility="collapsed",
        )

    c1, c2, c3 = st.columns(3)
    c1.button(
        "✅ Approve",
        key=f"approve_{tid}",
        type="primary",
        width="stretch",
        on_click=_approve_callback,
        args=(tid,),
    )
    c2.button(
        "🔄 Regenerate",
        key=f"regen_{tid}",
        width="stretch",
        on_click=_regenerate_callback,
        args=(tid,),
    )
    c3.button(
        "🚫 Reject",
        key=f"reject_{tid}",
        width="stretch",
        on_click=_reject_callback,
        args=(tid,),
    )
    st.divider()


def render_approval_phase() -> None:
    """Phase 3 — approve, edit, regenerate, or reject every generated draft,
    then send approved replies via engine.send_reply() — and book calendar
    meetings for meeting-request threads via calendar_engine.py."""
    st.header("✅ Approval Gate")
    st.caption(
        f"Phase {PHASES.index('Approval Gate') + 1} of {len(PHASES)} — tweak each draft "
        "in the editor if needed, then approve, regenerate, or reject it. Approving "
        "only marks a draft ready-to-send; nothing leaves your account until you "
        "press **📨 Send reply** on an approved draft (or **📅 Book Meeting** on a "
        "meeting request)."
    )

    # Pipeline run log: only visible right after a ⚡ pipeline run — X marks
    # every ERROR/FAILED entry (or ❌-prefixed one), check marks the rest.
    log_entries = st.session_state.pipeline_log
    if log_entries:
        n_errors = sum(
            1
            for entry in log_entries
            if "❌" in entry
            or "ERROR" in entry.upper()
            or "FAILED" in entry.upper()
        )
        title = f"⚡ Last pipeline run — {len(log_entries)} step(s)"
        if n_errors:
            title += f", {n_errors} error(s)"
        with st.expander(title, expanded=bool(n_errors)):
            for entry in log_entries:
                failed = (
                    "❌" in entry
                    or "ERROR" in entry.upper()
                    or "FAILED" in entry.upper()
                )
                st.write(f"{'❌' if failed else '✅'} {entry}")
            if st.button("🧹 Clear log", key="clear_pipeline_log_btn"):
                st.session_state.pipeline_log = []
                st.rerun()
        st.divider()

    drafts = st.session_state.drafts
    if not drafts:
        st.warning(
            "No drafts to review yet — generate drafts in **Draft Generation** first."
        )
        return

    pending = [
        tid
        for tid in drafts
        if tid not in st.session_state.approved and tid not in st.session_state.rejected
    ]

    # Running review count: X approved, Y rejected, Z pending, S sent.
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("✅ Approved", len(st.session_state.approved))
    c2.metric("🚫 Rejected", len(st.session_state.rejected))
    c3.metric("⏳ Pending", len(pending))
    c4.metric(
        "📨 Sent",
        len(st.session_state.sent),
        help="Approved drafts emailed via the gate's Send button.",
    )
    st.divider()

    if not pending:
        # Every draft has a verdict — celebrate once per review round.
        if not st.session_state.gate_celebrated:
            st.balloons()
            st.session_state.gate_celebrated = True
        st.success(
            f"All **{len(drafts)}** draft(s) reviewed — "
            f"**{len(st.session_state.approved)}** approved, "
            f"**{len(st.session_state.rejected)}** rejected. Head to "
            "**📤 Export Proof** (last phase, sidebar) to export the audit trail.",
            icon="🚦",
        )
    else:
        st.info(
            f"**{len(pending)}** draft(s) awaiting review — edits in each text box "
            "are what gets saved on **Approve**.",
            icon="🖋️",
        )

    triaged_by_id = {item.get("id"): item for item in st.session_state.triaged}
    for tid in pending:
        _render_review_card(tid, drafts.get(tid) or {}, triaged_by_id.get(tid))

    # Decisions so far, for the record. Approved drafts carry a 📨 Send button
    # until they are actually emailed; sent ones carry a badge instead.
    if st.session_state.approved or st.session_state.rejected:
        st.subheader("🗂️ Reviewed decisions")
        for rec_tid, rec in st.session_state.approved.items():
            is_sent = rec_tid in st.session_state.sent
            badge = " · 📨 Sent" if is_sent else ""
            item = triaged_by_id.get(rec_tid) or {}
            category = item.get("category")
            with st.expander(f"✅ {rec.get('thread_subject', rec.get('subject', rec_tid))}{badge}"):
                st.caption(
                    ("edited before approval" if rec.get("edited") else "approved as generated")
                    + f" · 🏷️ {category or 'unknown category'}"
                    + f" · {rec.get('approved_at', '')} · {rec.get('model', '')}"
                )
                send_error = st.session_state.pop(f"send_error_{rec_tid}", None)
                if send_error:
                    st.error(f"📨 Send failed: {send_error}")
                elif is_sent:
                    sent_line = (
                        f"Sent to **{rec.get('sent_to', '')}** at {rec.get('sent_at', '')}"
                    )
                    if rec.get("gmail_message_id"):
                        sent_line += f" · Gmail message `{rec['gmail_message_id']}`"
                    st.success(sent_line, icon="📨")
                st.markdown(rec.get("draft") or "_(empty)_")
                if not is_sent:
                    is_meeting = item.get("category") == "meeting-request"
                    # 📨 Send + 📅 Book Meeting side by side for every approved
                    # record — primary on meeting-request threads, secondary on
                    # the rest (the flow warns gracefully when a thread has no
                    # parseable meeting). Once a slot is booked, its calendar
                    # link takes the button's place (Send stays for the reply).
                    send_col, book_col = st.columns(2)
                    send_col.button(
                        "📨 Send reply",
                        key=f"send_{rec_tid}",
                        type="primary",
                        width="stretch",
                        on_click=_send_callback,
                        args=(rec_tid,),
                        help="Emails this approved draft from your Gmail account via "
                        "engine.send_reply() — the one step that leaves the Desk.",
                    )
                    booked = st.session_state.booked.get(rec_tid)
                    if booked:
                        with book_col:
                            _render_booked_state(booked)
                    elif book_col.button(
                        "📅 Book Meeting",
                        key=f"book_{rec_tid}",
                        type="primary" if is_meeting else "secondary",
                        width="stretch",
                        help=(
                            "Parses the meeting request, checks the proposed times "
                            "against your primary calendar, and books the first free "
                            "slot via calendar_engine.create_event() — attendees get "
                            "invitation emails."
                            if is_meeting
                            else "Not triaged as a meeting request, but you can still "
                            "try: the thread is parsed for proposed times and a "
                            "warning (not a booking) appears when none are found."
                        ),
                    ):
                        _book_meeting_flow(rec_tid, item)
        for rej_tid in st.session_state.rejected:
            rej_record = drafts.get(rej_tid) or {}
            with st.expander(f"🚫 {rej_record.get('subject', rej_tid)}"):
                st.caption("Draft rejected — it will not be exported.")
                st.markdown(rej_record.get("draft") or "_(empty)_")


def _collect_approved_threads() -> list[dict]:
    """Approved records joined with their original thread, in approval order.

    If a record's triage item or pipeline thread has fallen out of session
    state, a minimal stand-in is rebuilt from the record itself so the proof
    never silently drops an approved decision.
    """
    threads_by_id = {t.get("id"): t for t in st.session_state.threads}
    triaged_by_id = {item.get("id"): item for item in st.session_state.triaged}
    entries: list[dict] = []
    for tid, record in st.session_state.approved.items():
        item = triaged_by_id.get(tid)
        if item is None:
            item = {
                "id": tid,
                "subject": record.get("thread_subject", "(no subject)"),
                "sender": record.get("reply_to", "(unknown sender)"),
                "snippet": "",
            }
        entries.append(
            {"record": record, "item": item, "thread": _thread_for_drafting(item, threads_by_id)}
        )
    return entries


def generate_proof_markdown() -> str:
    """Build the Markdown proof-of-work document for every approved draft.

    Title + date header, then one section per approved thread: the original
    messages quoted, and the approved reply in a fenced code block.
    """
    entries = _collect_approved_threads()
    lines = [
        "# The Draft Desk — Proof of Work",
        "",
        f"**Date:** {datetime.now().astimezone().strftime('%A, %d %B %Y at %H:%M')}",
        "",
        f"**{len(entries)}** draft(s) approved at the Approval Gate · "
        f"**{len(st.session_state.rejected)}** rejected. Nothing was sent "
        "automatically — approving only marks a draft ready-to-send.",
        "",
    ]
    for i, entry in enumerate(entries, start=1):
        record, item, thread = entry["record"], entry["item"], entry["thread"]
        subject = record.get("thread_subject", "(no subject)")
        lines += [
            f"## {i}. {_bucket_icon(item.get('priority'))} {subject}",
            "",
            f"- **To:** {record.get('reply_to', '(unknown sender)')}",
            f"- **Priority:** {item.get('priority', '—')}",
            f"- **Model:** {record.get('model', '—')}",
            f"- **Approved:** {record.get('approved_at', '—')}"
            + (" *(edited before approval)*" if record.get("edited") else ""),
            "",
            "### Original thread",
            "",
        ]
        quoted: list[str] = []
        for msg in thread.get("messages") or []:
            body = (msg.get("body") or "").strip() or "_(empty body)_"
            block = "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
            header = f"> **✉️ {msg.get('from', '(unknown sender)')}**"
            if msg.get("date"):
                header += f" — {msg['date']}"
            quoted.append(f"{header}\n{block}")
        lines += [
            "\n>\n".join(quoted) if quoted else "> _(original thread unavailable)_",
            "",
            "### Approved reply",
            "",
            "```text",
            (record.get("draft") or "").strip() or "_(empty draft)_",
            "```",
            "",
            "---",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def generate_proof_html() -> str:
    """Build a dark-themed, social-shareable HTML proof-of-work document.

    One CSS-grid pair per approved thread: the original thread on the left
    (orange border) and the approved reply on the right (green border). A
    📋 Action Log section lists every sent/booked action from
    action_log.json (omitted when the log is empty). All dynamic content is
    HTML-escaped.
    """
    entries = _collect_approved_threads()
    e = html.escape

    def _msg_card_html(msg: dict) -> str:
        body = e((msg.get("body") or "").strip() or "_(empty body)_")
        date = f' <span class="msg-date">{e(msg["date"])}</span>' if msg.get("date") else ""
        return (
            '<div class="msg">'
            f'<div class="msg-from">✉️ {e(msg.get("from", "(unknown sender)"))}{date}</div>'
            f'<div class="msg-body">{body}</div>'
            "</div>"
        )

    cards: list[str] = []
    for i, entry in enumerate(entries, start=1):
        record, item, thread = entry["record"], entry["item"], entry["thread"]
        subject = record.get("thread_subject", "(no subject)")
        messages_html = "\n".join(_msg_card_html(m) for m in thread.get("messages") or [])
        if not messages_html:
            messages_html = '<div class="msg-body">_(original thread unavailable)_</div>'
        badge = ' <span class="badge">edited</span>' if record.get("edited") else ""
        cards.append(
            '  <section class="pair">\n'
            '    <div class="card thread-card">\n'
            '      <div class="card-label">Original thread</div>\n'
            f"      <h2>{i}. {_bucket_icon(item.get('priority'))} {e(subject)}</h2>\n"
            f'      <div class="meta">Priority: {e(str(item.get("priority", "—")))} · '
            f'To: {e(record.get("reply_to", "(unknown sender)"))}</div>\n'
            f"      {messages_html}\n"
            "    </div>\n"
            '    <div class="card draft-card">\n'
            f'      <div class="card-label">Approved reply{badge}</div>\n'
            f'      <div class="meta">{e(str(record.get("model", "")))} · '
            f'approved {e(str(record.get("approved_at", "")))}</div>\n'
            f'      <div class="draft-body">'
            f'{e((record.get("draft") or "").strip() or "_(empty draft)_")}</div>\n'
            "    </div>\n"
            "  </section>"
        )

    # Action log section: every send/book recorded at the Approval Gate.
    log_entries = get_action_log()
    log_rows: list[str] = []
    for log_entry in log_entries:
        action_type = str(log_entry.get("action_type", ""))
        icon = "📅" if action_type == "booked" else "📨"
        when = str(log_entry.get("timestamp", ""))
        try:
            when = datetime.fromisoformat(when).strftime("%b %d %I:%M %p")
        except (ValueError, TypeError):
            pass
        log_rows.append(
            '    <div class="log-row">'
            f'<span class="log-type">{icon} {e(action_type.upper())}</span>'
            f'<span class="log-subject">{e(str(log_entry.get("thread_subject", "(no subject)")))}</span>'
            f'<span class="log-detail"><code>{e(str(log_entry.get("detail", "")))}</code>'
            + (
                f' <span class="log-id">id: {e(str(log_entry.get("id", "")))}</span>'
                if log_entry.get("id")
                else ""
            )
            + "</span>"
            f'<span class="log-time">{e(when)}</span>'
            "</div>"
        )
    log_section = (
        '  <section class="log-section">\n'
        '    <div class="section-label">📋 Action Log — actions taken from the Approval Gate</div>\n'
        + "\n".join(log_rows)
        + "\n  </section>\n"
        if log_rows
        else ""
    )

    css = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #1a1a2e;
    color: #e8e8f0;
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    padding: 40px 24px;
    line-height: 1.55;
  }
  .wrap { max-width: 1080px; margin: 0 auto; }
  header { text-align: center; margin-bottom: 30px; }
  h1 { font-size: 1.9rem; color: #ffffff; letter-spacing: 0.5px; }
  .subtitle { color: #9fb0d8; margin-top: 6px; font-size: 0.95rem; }
  .stats { display: flex; gap: 10px; justify-content: center; margin-top: 14px; flex-wrap: wrap; }
  .stat { background: #16213e; border: 1px solid #2c3e6b; border-radius: 999px;
          padding: 6px 16px; font-size: 0.85rem; }
  .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 24px; }
  .card { background: #16213e; border-radius: 14px; padding: 18px 20px; }
  .thread-card { border: 2px solid #ff9f43; }
  .draft-card { border: 2px solid #2ed573; }
  .card-label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 1.5px;
                color: #9fb0d8; margin-bottom: 10px; }
  .thread-card .card-label { color: #ff9f43; }
  .draft-card .card-label { color: #2ed573; }
  h2 { font-size: 1.05rem; color: #ffffff; margin-bottom: 8px; }
  .meta { font-size: 0.8rem; color: #9fb0d8; margin-bottom: 12px; }
  .msg { background: #1a1a2e; border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; }
  .msg-from { font-weight: 600; font-size: 0.85rem; color: #cdd6f4; }
  .msg-date { font-weight: 400; color: #9fb0d8; margin-left: 8px; font-size: 0.78rem; }
  .msg-body { white-space: pre-wrap; font-size: 0.9rem; margin-top: 6px; }
  .draft-body { white-space: pre-wrap; font-family: Consolas, "Courier New", monospace;
                font-size: 0.86rem; color: #d1f5e3; }
  .badge { display: inline-block; background: #2ed573; color: #10241a; border-radius: 999px;
           padding: 1px 10px; font-size: 0.68rem; letter-spacing: 1px; text-transform: uppercase;
           margin-left: 8px; vertical-align: middle; }
  .log-section { margin-top: 10px; margin-bottom: 24px; }
  .section-label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 1.5px;
                   color: #9fb0d8; margin-bottom: 12px; }
  .log-row { display: grid; grid-template-columns: 120px 1fr auto 130px; gap: 14px;
             align-items: center; background: #16213e; border: 1px solid #2c3e6b;
             border-radius: 10px; padding: 10px 14px; margin-bottom: 8px; }
  .log-type { font-weight: 600; font-size: 0.8rem; letter-spacing: 1px; color: #cdd6f4; }
  .log-subject { font-size: 0.9rem; color: #ffffff; }
  .log-detail code { background: #0f3460; color: #d1f5e3; border-radius: 6px;
                     padding: 3px 10px; font-family: Consolas, "Courier New", monospace;
                     font-size: 0.82rem; }
  .log-id { color: #9fb0d8; font-size: 0.72rem; margin-left: 8px; }
  .log-time { font-size: 0.78rem; color: #9fb0d8; text-align: right; }
  footer { text-align: center; color: #9fb0d8; font-size: 0.8rem; margin-top: 26px; }
  @media (max-width: 760px) { .pair { grid-template-columns: 1fr; } }
"""
    date_str = datetime.now().astimezone().strftime("%A, %d %B %Y at %H:%M")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Draft Desk — Proof of Work</title>
<style>{css}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>✍️ The Draft Desk — Proof of Work</h1>
    <div class="subtitle">{date_str} · Chief-of-Staff pipeline · human-in-the-loop gate</div>
    <div class="stats">
      <span class="stat">✅ {len(entries)} approved</span>
      <span class="stat">🚫 {len(st.session_state.rejected)} rejected</span>
      <span class="stat">🛡️ 0 sent automatically</span>
    </div>
  </header>
{"".join(cards)}
{log_section}
  <footer>Approve only marks a draft ready-to-send — nothing was emailed automatically.</footer>
</div>
</body>
</html>
"""


def _render_approved_pair(record: dict, item: dict | None, thread: dict) -> None:
    """Side-by-side proof preview: original thread left, approved draft right."""
    record = record or {}
    subject = record.get("thread_subject", "(no subject)")
    icon = _bucket_icon((item or {}).get("priority"))

    left, right = st.columns(2)
    with left:
        with st.expander(f"{icon} {subject} — {record.get('reply_to', '(unknown sender)')}"):
            messages = thread.get("messages") or []
            if not messages:
                st.caption("_(original thread unavailable)_")
            for i, msg in enumerate(messages):
                st.markdown(f"**✉️ {msg.get('from', '(unknown sender)')}**")
                if msg.get("date"):
                    st.caption(msg["date"])
                st.markdown((msg.get("body") or "_(empty body)_").strip())
                if i < len(messages) - 1:
                    st.divider()
    with right:
        with st.expander(f"✅ Approved draft — {record.get('model', 'LLM')}", expanded=True):
            edited = "edited before approval" if record.get("edited") else "approved as generated"
            st.caption(f"{edited} · {record.get('approved_at', '')}")
            st.markdown((record.get("draft") or "").strip() or "_(empty)_")


def render_export_phase() -> None:
    """Phase 4 — preview and export the approved-draft audit trail."""
    st.header("📤 Export Proof")
    st.caption(
        f"Phase {PHASES.index('Export Proof') + 1} of {len(PHASES)} — export the "
        "proof-of-work audit trail for every draft approved at the gate."
    )

    approved = st.session_state.approved
    if not approved:
        rejected_note = (
            f" {len(st.session_state.rejected)} draft(s) were rejected and are not exported."
            if st.session_state.rejected
            else ""
        )
        st.warning(
            "Nothing to export yet — no drafts have been approved. "
            f"Approve drafts in the **✅ Approval Gate** first.{rejected_note}"
        )
        return

    rejected_note = (
        f" · {len(st.session_state.rejected)} rejected (not included)."
        if st.session_state.rejected
        else "."
    )
    st.info(
        f"**{len(approved)}** approved draft(s) ready to export{rejected_note}",
        icon="🧾",
    )

    # Preview: original thread | approved draft, side by side.
    for entry in _collect_approved_threads():
        _render_approved_pair(entry["record"], entry["item"], entry["thread"])

    # Action log: every send/book the gate performed, file order = oldest first.
    st.divider()
    st.subheader("Action Log")
    log_entries = get_action_log()
    if not log_entries:
        st.info("No actions logged yet.")
    else:
        for log_entry in log_entries:
            action_type = str(log_entry.get("action_type", ""))
            icon = "📅" if action_type == "booked" else "📨"
            try:
                when = datetime.fromisoformat(
                    str(log_entry.get("timestamp", ""))
                ).strftime("%b %d %I:%M %p")
            except (ValueError, TypeError):
                when = str(log_entry.get("timestamp", ""))
            c_type, c_subject, c_detail, c_when = st.columns(4)
            c_type.markdown(f"{icon} **{action_type.upper()}**")
            c_subject.markdown(f"**{log_entry.get('thread_subject', '(no subject)')}**")
            c_detail.markdown(f"`{log_entry.get('detail', '')}`")
            c_when.caption(when)

    st.divider()
    st.subheader("Download the audit trail")
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M")
    c1, c2 = st.columns(2)
    c1.download_button(
        "⬇️ Download Proof (Markdown)",
        data=generate_proof_markdown().encode("utf-8"),
        file_name=f"draft-desk-proof-{stamp}.md",
        mime="text/markdown",
        key="download_proof_md",
        type="primary",
        width="stretch",
    )
    c2.download_button(
        "⬇️ Download Proof (HTML)",
        data=generate_proof_html().encode("utf-8"),
        file_name=f"draft-desk-proof-{stamp}.html",
        mime="text/html",
        key="download_proof_html",
        width="stretch",
    )
    st.caption(
        "The Markdown version is a plain audit trail; the HTML version is a styled, "
        "shareable page. Both cover approved drafts only — rejected drafts never "
        "leave the gate."
    )


PHASE_RENDERERS = {
    "Inbox & Triage": render_inbox_phase,
    "Draft Generation": render_draft_phase,
    "Approval Gate": render_approval_phase,
    "Export Proof": render_export_phase,
}


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #

def _goto_phase(phase: str) -> None:
    """on_click callback for the sidebar workflow buttons (runs pre-rerun)."""
    st.session_state.current_phase = phase


def render_sidebar() -> None:
    """Sidebar — identity, the ⚡ one-click pipeline, source picker, nav."""
    with st.sidebar:
        st.title("✍️ The Draft Desk")
        st.caption("Your AI chief of staff — triage the inbox, draft replies, gate approvals, export the proof.")
        st.divider()

        # The one-click pipeline: sets the flag and reruns — main() then
        # routes this run's content area to _render_pipeline_execution().
        if st.button(
            "⚡ Run Full Pipeline",
            key="run_full_pipeline_btn",
            type="primary",
            width="stretch",
            help="Fetch → triage → draft in one go, with live progress — lands you "
            "at the Approval Gate with every urgent/needs-reply thread drafted.",
        ):
            st.session_state.pipeline_running = True
            st.rerun()
        st.caption("Fetches, triages, and drafts — stops at Approval Gate.")

        st.radio(
            "Source",
            [SOURCE_GMAIL, SOURCE_SAMPLE],
            key="source",
            help="Gmail pulls live threads through engine.py (MCP server when "
            "available, raw Gmail API as the deployment fallback). "
            "Sample threads load sample_threads.json — no Gmail needed.",
        )

        st.divider()
        st.subheader("Workflow")
        current = st.session_state.current_phase
        for idx, phase in enumerate(PHASES):
            st.button(
                phase,
                key=f"nav_{idx}",
                on_click=_goto_phase,
                args=(phase,),
                width="stretch",
                type="primary" if phase == current else "secondary",
            )

        st.divider()
        st.caption(
            f"Current phase: **{st.session_state.current_phase}**  \n"
            f"Threads: {len(st.session_state.threads)} · "
            f"Drafts: {len(st.session_state.drafts)} · "
            f"Approved: {len(st.session_state.approved)} · "
            f"Rejected: {len(st.session_state.rejected)}"
        )


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def render_phase() -> None:
    """Dispatch the current phase to its renderer."""
    PHASE_RENDERERS.get(st.session_state.current_phase, render_inbox_phase)()


def main() -> None:
    """Route the main content area.

    While the ⚡ pipeline is running, its live st.status progress UI takes
    over the page — and it ends with its own st.rerun(), so no phase renders
    in that pass and the run lands on the Approval Gate when it finishes.
    A FATAL run lands back on the Inbox instead — this warning is the only
    persistent trace of what went wrong (the status container is transient).
    """
    # Cloud deployments keep every credential in st.secrets; bridge them into
    # the environment/files the pipeline expects (a no-op locally, where the
    # real files already exist and st.secrets is absent).
    try:
        from bootstrap import ensure_runtime_credentials

        ensure_runtime_credentials()
    except Exception:  # noqa: BLE001 — bootstrap must never take the app down
        pass

    error = st.session_state.get("pipeline_error")
    if error:
        # The hint below must match WHY the run failed: the quota text is only
        # right for LLM-related failures — a missing Gmail MCP server or an
        # unprovisioned OAuth grant is a credentials/deployment problem, and
        # showing "add OpenRouter credits" for it just misleads.
        lowered = error.lower()
        quota_related = any(
            marker in lowered
            for marker in (
                "quota", "429", "rate limit", "credit", "402", "401",
                "openrouter", "gemini", "llm", "api key", "backends failed",
                "empty response",
            )
        )
        if quota_related:
            hint = (
                "\n\n**Most common cause: the LLM free-tier quota is exhausted** "
                "(OpenRouter free models: ~50/day; Gemini free tier: 20/day for "
                "gemini-3.7-flash). Wait for the daily reset, add OpenRouter "
                "credits, or point `OPENROUTER_MODEL` in `.env` at a paid model — "
                "then press **⚡ Run Full Pipeline** again."
            )
        else:
            hint = (
                "\n\n**Most likely cause on a deployment:** this machine has no "
                "Gmail MCP server and no provisioned credentials. Fetch falls "
                "back to the raw Gmail API, which needs `GOOGLE_TOKEN_JSON`, and "
                "triage/drafts need `OPENROUTER_API_KEY` or `GEMINI_API_KEY` — "
                "add them in this app's **Secrets** settings (see the "
                "`bootstrap.py` docstring for the exact names), then press "
                "**⚡ Run Full Pipeline** again. Locally nothing is needed — "
                "the `.env` / `token.json` / MCP server files already exist."
            )
        st.warning(
            "⚡ The last full-pipeline run stopped early:\n\n"
            + error
            + hint,
            icon="⚠️",
        )
    if st.session_state.pipeline_running:
        _render_pipeline_execution()
    else:
        render_phase()


render_sidebar()
main()



