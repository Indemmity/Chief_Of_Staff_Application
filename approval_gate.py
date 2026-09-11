"""
approval_gate.py — Human-in-the-Loop approval gate for the AI email
ghostwriter (Chief-of-Staff pipeline, final stage).

GUARDRAIL: the AI never sends anything by itself. Every draft must pass
through one explicit human decision here:

    APPROVE  — marks the draft "ready to send" and appends it (with a
               timestamp) to approved_drafts.json. It does NOT send.
    EDIT     — opens an editor pre-filled with the draft; the edited text
               goes through the same approve path (logged as edited).
    REJECT   — discards the draft; you can regenerate with one click.

Layout: the sidebar picks the thread (3 samples or pasted custom JSON),
the main area shows the thread history (left) and the AI draft (right)
with the three action buttons underneath the draft.

API key resolution (both names checked, in order):
    1. st.secrets["OPENROUTER_API_KEY"] / st.secrets["GEMINI_API_KEY"]
    2. environment: OPENROUTER_API_KEY / GEMINI_API_KEY (the .env next to
       llm_client.py is loaded automatically on import)
    3. a password field in the sidebar (session-only, never written out)

Note: llm_client.py in this workspace drafts via OpenRouter with
OPENROUTER_API_KEY; GEMINI_API_KEY is the automatic fallback backend, so
either name reaches a backend.

Run with:
    streamlit run approval_gate.py
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from context_builder import assemble_context
from draft_machine import MODEL, SAMPLE_THREADS, draft_reply_with_metadata

WORKSPACE_DIR = Path(__file__).resolve().parent
APPROVED_DRAFTS_PATH = WORKSPACE_DIR / "approved_drafts.json"

# Session-state statuses
STATUS_NONE = "none"          # no draft yet / fresh draft on display
STATUS_APPROVED = "approved"  # draft marked ready-to-send and logged
STATUS_EDITING = "editing"    # edit text area open over the current draft
STATUS_REJECTED = "rejected"  # draft discarded — regenerate to continue

CUSTOM_CSS = """
<style>
    .stApp { background-color: #1a1a2e; }
    h1, h2, h3 { color: #eef2fb; }
    [data-testid="stSidebar"] {
        background-color: #12122b;
        border-right: 1px solid #24244a;
    }
    .guardrail {
        background: rgba(233, 69, 96, 0.10);
        border: 1px solid #e94560;
        border-radius: 10px;
        padding: 10px 14px;
        margin-bottom: 14px;
        color: #ffd9e0;
        font-size: 0.86rem;
    }
    .thread-box {
        background: #16213e;
        border: 1px solid #24345e;
        border-left: 4px solid #4f7cff;
        border-radius: 10px;
        padding: 12px 16px;
        margin-bottom: 12px;
    }
    .thread-last {
        border-left: 4px solid #e94560;
        background: #1c2749;
    }
    .reply-badge {
        display: inline-block;
        background: #e94560;
        color: #10131f;
        font-size: 0.62rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        border-radius: 4px;
        padding: 2px 7px;
        margin-bottom: 8px;
    }
    .thread-meta {
        display: flex;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 4px;
        margin-bottom: 6px;
    }
    .thread-sender { color: #cdd6f4; font-weight: 600; font-size: 0.90rem; }
    .thread-date { color: #8892b0; font-size: 0.76rem; }
    .thread-body {
        color: #d7dce8;
        font-size: 0.92rem;
        line-height: 1.55;
        white-space: pre-wrap;
    }
    .draft-box {
        background: #0f3460;
        border: 1px solid #2b5cb8;
        border-radius: 12px;
        padding: 18px 20px;
        color: #eef2fb;
        font-size: 0.95rem;
        line-height: 1.6;
        white-space: pre-wrap;
    }
    .draft-meta { color: #9fb0d8; font-size: 0.80rem; margin-bottom: 8px; }
</style>
"""

# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #


def _init_state() -> None:
    """Create every session-state key the gate relies on (idempotent)."""
    defaults: dict[str, Any] = {
        "draft": None,            # current draft text (str) or None
        "draft_meta": None,       # dict from draft_reply_with_metadata
        "active_thread": None,    # thread the current draft replies to
        "status": STATUS_NONE,    # none | approved | editing | rejected
        "generation_count": 0,    # drafts generated this session
        "edited_text": "",        # buffer for the edit text area
        "manual_api_key": "",     # sidebar password field (session only)
        "approved_record": None,  # record written on the last approve
        "preview_thread": None,   # thread currently picked in the sidebar
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


# --------------------------------------------------------------------------- #
# API key resolution
# --------------------------------------------------------------------------- #


def _get_secret(name: str) -> str | None:
    """Read a key from st.secrets, tolerating the absence of secrets files."""
    try:
        if name in st.secrets:
            value = st.secrets[name]
            return str(value) if value else None
    except Exception:
        pass  # no secrets.toml and not on Streamlit Cloud — fall through
    return None


def resolve_api_key() -> tuple[str | None, str]:
    """
    Resolution order for the drafting key:

        1. st.secrets: OPENROUTER_API_KEY, then GEMINI_API_KEY
        2. environment: OPENROUTER_API_KEY, then GEMINI_API_KEY
           (llm_client.py dotenv-loads the .env next to it on import,
           so a key defined there already shows up here)

    Returns (key, human-readable source), or (None, "") when nothing is set.
    """
    for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY"):
        value = _get_secret(name)
        if value:
            return value, f"st.secrets[{name}]"
    for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value, name
    return None, ""


def _drafting_key_available() -> bool:
    """True when a key exists under a name llm_client.py reads."""
    return any(
        _get_secret(name) or os.environ.get(name)
        for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY")
    )


def _apply_manual_key() -> None:
    """Push the sidebar password-field value into the environment under the
    name the primary drafting backend actually reads (OPENROUTER_API_KEY).
    Session-only: nothing is ever written to disk."""
    key = (st.session_state.get("manual_api_key") or "").strip()
    if key:
        os.environ["OPENROUTER_API_KEY"] = key


def _ensure_backend_keys() -> bool:
    """Guarantee at least one drafting key (OPENROUTER_API_KEY or
    GEMINI_API_KEY) is set in os.environ before a generation call, falling
    back to the sidebar password field. Returns True when ready."""
    for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY"):
        value = _get_secret(name) or os.environ.get(name)
        if value:
            os.environ[name] = str(value)
            return True
    manual = (st.session_state.get("manual_api_key") or "").strip()
    if manual:
        os.environ["OPENROUTER_API_KEY"] = manual
        return True
    return False


# --------------------------------------------------------------------------- #
# Data helpers
# --------------------------------------------------------------------------- #


def parse_custom_thread(raw: str) -> tuple[dict | None, str | None]:
    """Validate pasted thread JSON. Returns (thread, error_message)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON — {exc.msg} (line {exc.lineno})"
    if not isinstance(data, dict):
        return None, "expected a JSON object with 'subject' and 'messages'"
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return None, "'messages' must be a non-empty list"
    for i, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            return None, f"message {i} must be an object"
        if not str(message.get("from", "")).strip():
            return None, f"message {i} is missing 'from'"
        if not str(message.get("body", "")).strip():
            return None, f"message {i} is missing 'body'"
    data.setdefault("subject", "(no subject)")
    return data, None


def read_approved_records() -> list:
    """Load approved_drafts.json as a list (tolerates missing/corrupt files)."""
    if not APPROVED_DRAFTS_PATH.exists():
        return []
    try:
        data = json.loads(APPROVED_DRAFTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_approved_draft(record: dict) -> None:
    """Append an approval record to approved_drafts.json. Raises OSError on
    write failure (the caller surfaces it in the UI)."""
    records = read_approved_records()
    records.append(record)
    APPROVED_DRAFTS_PATH.write_text(
        json.dumps(records, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Actions (mutate session state)
# --------------------------------------------------------------------------- #


def generate_draft(thread: dict) -> bool:
    """Call draft_machine for `thread` and load the result into session
    state. Returns True on success (callers then st.rerun())."""
    if not _ensure_backend_keys():
        st.error(
            "No drafting key available — add one under **🔑 3 · API key** in "
            "the sidebar, or put OPENROUTER_API_KEY (or GEMINI_API_KEY) in "
            "`.streamlit/secrets.toml` or the `.env` next to llm_client.py."
        )
        return False
    try:
        with st.spinner("Ghostwriting a reply…"):
            result = draft_reply_with_metadata(thread)
    except Exception as exc:  # surface provider/key errors in the UI
        st.error(f"**Draft generation failed**\n\n{exc}")
        return False

    st.session_state.draft = result["draft"]
    st.session_state.draft_meta = result
    st.session_state.active_thread = thread
    st.session_state.generation_count += 1
    st.session_state.status = STATUS_NONE
    st.session_state.edited_text = ""
    return True


def approve_draft(final_text: str, *, edited: bool) -> None:
    """Mark `final_text` ready-to-send and append it to approved_drafts.json.
    This NEVER sends anything — it only logs."""
    meta = st.session_state.draft_meta or {}
    record = {
        "approved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "ready_to_send",
        "edited": edited,
        "generation": st.session_state.generation_count,
        "thread_subject": meta.get("subject", "(no subject)"),
        "reply_to": meta.get("reply_to", "(unknown sender)"),
        "model": meta.get("model", MODEL),
        "draft": final_text,
    }
    try:
        save_approved_draft(record)
    except OSError as exc:
        st.error(f"Could not write {APPROVED_DRAFTS_PATH.name}: {exc}")
        return
    st.session_state.approved_record = record
    st.session_state.status = STATUS_APPROVED
    st.rerun()


# --------------------------------------------------------------------------- #
# Rendering — main area
# --------------------------------------------------------------------------- #


def render_guardrail_banner() -> None:
    st.markdown(
        '<div class="guardrail">🛡️ <b>HUMAN-IN-THE-LOOP GATE</b> — drafts are '
        "never sent automatically. <b>APPROVE</b> only marks a draft "
        "<i>ready-to-send</i> and logs it to <code>approved_drafts.json</code>; "
        "you send it yourself from your mail client.</div>",
        unsafe_allow_html=True,
    )


def render_thread_column(thread: dict) -> None:
    """Left column: the full email thread, one styled box per message."""
    st.subheader("📨 Thread")
    st.markdown(
        '<div class="draft-meta">Subject: <b>'
        f'{html.escape(str(thread.get("subject", "(no subject)")))}</b></div>',
        unsafe_allow_html=True,
    )
    messages = thread.get("messages", [])
    parts: list[str] = []
    for i, message in enumerate(messages):
        is_last = i == len(messages) - 1
        box_class = "thread-box thread-last" if is_last else "thread-box"
        badge = (
            '<span class="reply-badge">↩ REPLYING TO THIS</span>'
            if is_last
            else ""
        )
        parts.append(
            f'<div class="{box_class}">{badge}'
            '<div class="thread-meta">'
            '<span class="thread-sender">👤 '
            f'{html.escape(str(message.get("from", "(unknown sender)")))}</span>'
            f'<span class="thread-date">🗓 {html.escape(str(message.get("date", "—")))}</span>'
            "</div>"
            f'<div class="thread-body">{html.escape(str(message.get("body", "")))}</div>'
            "</div>"
        )
    st.markdown("".join(parts), unsafe_allow_html=True)

    with st.expander("🧠 What the AI sees (assembled prompt)"):
        context = assemble_context(thread)
        st.caption("System prompt — persona, writing rules, few-shot examples")
        st.code(context["system"], language="text")
        st.caption("User prompt — the thread plus the drafting ask")
        st.code(context["user"], language="text")


def _render_mismatch_warning() -> None:
    """Warn when the sidebar selection no longer matches the drafted thread."""
    meta = st.session_state.draft_meta
    thread = st.session_state.get("preview_thread")
    if meta and thread and meta.get("subject") != thread.get("subject"):
        st.warning(
            f'⚠️ This draft was generated for “{meta.get("subject")}” but the '
            f'sidebar now points at “{thread.get("subject")}” — regenerate '
            "before approving."
        )


def _render_action_buttons() -> None:
    """APPROVE / EDIT / REJECT under the fresh draft."""
    col_ok, col_edit, col_no = st.columns(3)
    with col_ok:
        if st.button(
            "✅ APPROVE",
            width="stretch",
            type="primary",
            help="Mark ready-to-send and log to approved_drafts.json (does NOT send)",
        ):
            approve_draft(st.session_state.draft, edited=False)
    with col_edit:
        if st.button(
            "✏️ EDIT",
            width="stretch",
            help="Tweak the draft by hand, then approve the edited version",
        ):
            st.session_state.edited_text = st.session_state.draft or ""
            st.session_state.status = STATUS_EDITING
            st.rerun()
    with col_no:
        if st.button(
            "🔴 REJECT",
            width="stretch",
            help="Discard this draft",
        ):
            st.session_state.draft = None
            st.session_state.draft_meta = None
            st.session_state.status = STATUS_REJECTED
            st.rerun()
    st.caption(
        "⬆️ Approving does **not** send anything — it only marks the draft "
        "ready and logs it."
    )


def render_draft_column() -> None:
    """Right column: the draft plus status-driven UI for every state."""
    st.subheader("✍️ AI Draft")
    status = st.session_state.status

    if status == STATUS_NONE and st.session_state.draft:
        _render_mismatch_warning()
        meta = st.session_state.draft_meta or {}
        st.markdown(
            '<div class="draft-meta">'
            f'Draft #{st.session_state.generation_count} · '
            f'model: {html.escape(str(meta.get("model", MODEL)))} · '
            f'to: {html.escape(str(meta.get("reply_to", "—")))}</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="draft-box">{html.escape(st.session_state.draft)}</div>',
            unsafe_allow_html=True,
        )
        _render_action_buttons()

    elif status == STATUS_EDITING:
        _render_mismatch_warning()
        st.warning(
            "✏️ **EDITING** — modify the draft below, then approve the "
            "edited version."
        )
        st.text_area("Edited draft", height=280, key="edited_text")
        col_ok, col_cancel = st.columns(2)
        with col_ok:
            if st.button(
                "✅ Approve edited version", width="stretch", type="primary"
            ):
                edited = (st.session_state.get("edited_text") or "").strip()
                if not edited:
                    st.warning("The edited draft is empty — write something or cancel.")
                else:
                    approve_draft(edited, edited=True)
        with col_cancel:
            if st.button("✖ Cancel editing", width="stretch"):
                st.session_state.status = STATUS_NONE
                st.session_state.edited_text = ""
                st.rerun()

    elif status == STATUS_APPROVED and st.session_state.approved_record:
        record = st.session_state.approved_record
        suffix = " (edited version)" if record.get("edited") else ""
        st.success(
            f"✅ **APPROVED — READY TO SEND**{suffix} · saved to "
            f"`{APPROVED_DRAFTS_PATH.name}` at {record['approved_at']}"
        )
        st.markdown(
            f'<div class="draft-box">{html.escape(str(record["draft"]))}</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "The gate never sends. Copy this into your mail client — it is "
            "logged in approved_drafts.json and waiting for you."
        )

    elif status == STATUS_REJECTED:
        st.error(
            "❌ **REJECTED** — the draft was discarded. Nothing was saved "
            "or sent."
        )
        if st.button("🔄 Regenerate draft", width="stretch"):
            thread = st.session_state.get("preview_thread")
            if thread and generate_draft(thread):
                st.rerun()
        st.caption(
            "…or pick another thread in the sidebar and hit **✨ Generate Draft**."
        )

    else:
        st.info(
            "No draft yet — pick a thread in the sidebar and hit "
            "**✨ Generate Draft**."
        )


# --------------------------------------------------------------------------- #
# Rendering — sidebar
# --------------------------------------------------------------------------- #


def render_key_panel() -> None:
    """Sidebar API-key status + manual password-field fallback (spec item 5)."""
    st.subheader("🔑 3 · API key")
    key, source = resolve_api_key()
    if _drafting_key_available():
        st.success(f"Drafting key ready — found `{source}`.")
    elif key:
        st.warning(
            f"`{source}` found, but it is not a name the drafting backend "
            "reads (`OPENROUTER_API_KEY` / `GEMINI_API_KEY`). Enter a key "
            "below — OpenRouter keys are free at openrouter.ai/keys."
        )
    else:
        st.error("No API key found.")
    st.text_input(
        "Paste a key to use for this session",
        type="password",
        key="manual_api_key",
        on_change=_apply_manual_key,
        placeholder="kept in memory only, never written to disk",
    )
    st.caption(
        "Checked in order: `st.secrets['OPENROUTER_API_KEY']` → "
        "`st.secrets['GEMINI_API_KEY']` → `OPENROUTER_API_KEY`/`GEMINI_API_KEY` in "
        "the environment (the `.env` next to llm_client.py is loaded "
        "automatically) → this password field."
    )


def render_sidebar() -> None:
    """Thread selection (samples or custom JSON), generate button, key panel."""
    with st.sidebar:
        st.subheader("📬 1 · Pick a thread")
        labels = [
            f"{i + 1}. {t.get('subject', '(no subject)')}"
            for i, t in enumerate(SAMPLE_THREADS)
        ]
        st.selectbox(
            "Sample threads",
            options=list(range(len(labels))),
            format_func=lambda i: labels[i],
            key="selected_thread",
        )
        st.text_area(
            "…or paste a custom thread JSON",
            height=180,
            key="custom_thread_json",
            placeholder=(
                '{\n  "subject": "...",\n  "messages": [\n'
                '    {"from": "a@x.com", "date": "…", "body": "…"}\n  ]\n}'
            ),
        )
        raw = (st.session_state.get("custom_thread_json") or "").strip()
        if raw:
            thread, error = parse_custom_thread(raw)
            if error:
                st.error(f"Custom thread JSON — {error}")
                thread = SAMPLE_THREADS[st.session_state.selected_thread]
            else:
                st.caption("✓ Using the pasted custom thread for the next draft.")
        else:
            thread = SAMPLE_THREADS[st.session_state.selected_thread]
        st.session_state.preview_thread = thread

        st.subheader("🚀 2 · Generate")
        if st.button("✨ Generate Draft", type="primary", width="stretch"):
            if generate_draft(thread):
                st.rerun()
        if st.session_state.generation_count:
            st.caption(
                f"Generated this session: **{st.session_state.generation_count}**"
                " draft(s)"
            )

        st.divider()
        render_key_panel()

        st.divider()
        st.markdown(f"📜 **Approved drafts:** {len(read_approved_records())}")
        st.caption(f"Appended to `{APPROVED_DRAFTS_PATH.name}` next to this script.")


# --------------------------------------------------------------------------- #
# App entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    st.set_page_config(page_title="Approval Gate", page_icon="🛡️", layout="wide")
    _init_state()
    _apply_manual_key()  # re-apply the sidebar key after every rerun
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    st.title("🛡️ Approval Gate")
    st.caption(
        "Human-in-the-loop for the AI email ghostwriter — a draft never "
        "leaves this screen without your explicit decision."
    )
    render_guardrail_banner()
    render_sidebar()

    left, right = st.columns(2, gap="large")
    with left:
        render_thread_column(st.session_state.preview_thread or SAMPLE_THREADS[0])
    with right:
        render_draft_column()


if __name__ == "__main__":
    main()





