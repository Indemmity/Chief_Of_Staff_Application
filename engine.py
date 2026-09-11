"""
engine.py — Gmail inbox thread fetching via the Gmail MCP server.

`fetch_threads()` launches the Gmail MCP server (the same
`@gongrzhe/server-gmail-autoauth-mcp` server that Cline uses), speaks
JSON-RPC 2.0 over stdio with it, and returns the most recent inbox
threads as plain Python dicts:

    [
        {
            "thread_id": "1a06f3da0428c156",
            "sender":    "\\"Cline Bot Inc.\\" <invoice+...@stripe.com>",
            "subject":   "Your receipt from Cline Bot Inc. #2568-1428",
            "snippet":   "Cline Bot Inc. Receipt from Cline Bot Inc. ...",
            "date":      "Sat, 5 Sep 2026 01:45:04 +0000",
        },
        ...
    ]

No third-party Python packages are required: the MCP session is a plain
subprocess exchanging newline-delimited JSON-RPC messages, so this works
with the stock standard library on any modern Python.

The one exception is send_reply(): it sends through the raw Gmail API
(googleapiclient) instead of the MCP server and therefore needs the
google-api-python-client package installed.

Server launch order:
  1. A `gmail` entry in Cline's MCP settings file
     (%APPDATA%\\Code\\User\\globalStorage\\saoudrizwan.claude-dev\\settings\\cline_mcp_settings.json),
     if one is registered there.
  2. Fallback: `node gmail-mcp-server/dist/index.js` from this workspace
     (the local clone of the very server Cline runs).
  3. Raw Gmail API fallback: when neither MCP option can even be LAUNCHED —
     the case on fresh cloud deployments (e.g. Streamlit Community Cloud
     clones only this repo, and gmail-mcp-server/ is deliberately not in it) —
     fetch_threads() switches to the same googleapiclient grant (token.json)
     that send_reply() already uses. See _fetch_threads_raw().

OAuth is handled entirely by the server process itself: it reads the
refresh token from ~/.gmail-mcp/credentials.json, so no browser
re-authentication is triggered as long as that token is still valid.

Run as a script (`python engine.py`), it runs the full pipeline through
`print(run_pipeline(2))`: fetch 2 inbox threads, classify them with
triage.triage_inbox (LLM backend), print an "INBOX DIGEST" grouped by
priority, then print the returned results list.
"""

from __future__ import annotations

import base64
import html
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
from collections import deque
from datetime import date
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from triage import triage_inbox

# --------------------------------------------------------------------------- #
# IPv4 monkey-patch (identical to calendar_engine.py)
# --------------------------------------------------------------------------- #

# Some networks resolve googleapis.com to unreachable IPv6 addresses, which
# surfaces in googleapiclient as long timeouts / connection errors. Forcing
# AF_INET makes every socket opened by this process IPv4-only.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

__all__ = ["fetch_threads", "send_reply", "format_digest", "run_pipeline", "GmailMcpClient"]

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #

WORKSPACE_DIR = Path(__file__).resolve().parent
SERVER_DIR = WORKSPACE_DIR / "gmail-mcp-server"
SERVER_ENTRY = SERVER_DIR / "dist" / "index.js"

CLINE_MCP_SETTINGS = (
    Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    / "Code"
    / "User"
    / "globalStorage"
    / "saoudrizwan.claude-dev"
    / "settings"
    / "cline_mcp_settings.json"
)

MCP_PROTOCOL_VERSION = "2024-11-05"  # protocol generation used by this server build
CLIENT_INFO = {"name": "chief-of-staff-engine", "version": "1.0.0"}

DEFAULT_TIMEOUT = 60.0   # seconds to wait for any single MCP response
SNIPPET_MAX_CHARS = 200  # length of the snippet returned per thread
GMAIL_USER_ID = "me"     # Gmail API alias for the authenticated user (send_reply)


def _resolve_server_launch() -> tuple[list[str], dict[str, str], Path | None]:
    """
    Decide how to launch the Gmail MCP server.

    Returns (command, extra_env, cwd). Prefers a `gmail` entry registered in
    Cline's MCP settings; falls back to the local gmail-mcp-server clone.
    """
    # 1) A gmail server registered in Cline's MCP settings, if present.
    try:
        settings = json.loads(CLINE_MCP_SETTINGS.read_text(encoding="utf-8"))
        servers: dict[str, Any] = settings.get("mcpServers", {})
        for name, cfg in servers.items():
            if "gmail" in name.lower() and isinstance(cfg, dict) and cfg.get("command"):
                command = [str(cfg["command"])]
                for arg in cfg.get("args", []):
                    arg = str(arg)
                    if not Path(arg).exists() and (SERVER_DIR / arg).exists():
                        arg = str(SERVER_DIR / arg)  # resolve relative args
                    command.append(arg)
                if Path(command[0]).name.lower() in {"npx", "npx.cmd"}:
                    resolved = shutil.which("npx") or shutil.which("npx.cmd")
                    if resolved:
                        command[0] = resolved
                return command, {str(k): str(v) for k, v in cfg.get("env", {}).items()}, None
    except (OSError, ValueError):
        pass  # settings file missing, unreadable or malformed -> use fallback

    # 2) Local clone of the Gmail MCP server in this workspace.
    if not SERVER_ENTRY.is_file():
        raise FileNotFoundError(
            f"Gmail MCP server entry not found at {SERVER_ENTRY}.\n"
            f"Run `npm install && npm run build` inside {SERVER_DIR}, or register "
            "the server in Cline's MCP settings (cline_mcp_settings.json)."
        )
    node = shutil.which("node") or "node"
    return [node, str(SERVER_ENTRY)], {}, SERVER_DIR


# --------------------------------------------------------------------------- #
# Minimal MCP client (JSON-RPC 2.0 over stdio)
# --------------------------------------------------------------------------- #


class _StdoutReader(threading.Thread):
    """Drains the server's stdout into a queue so lines can be read with timeouts."""

    def __init__(self, stream) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self.lines: "queue.Queue[str | None]" = queue.Queue()

    def run(self) -> None:
        try:
            for line in self._stream:
                self.lines.put(line)
        except Exception:  # noqa: BLE001 - the reader must never crash the client
            pass
        finally:
            self.lines.put(None)  # EOF sentinel


class GmailMcpClient:
    """
    Minimal MCP client for the Gmail MCP server.

    Usage:
        with GmailMcpClient() as client:
            text = client.call_tool("search_emails", {"query": "in:inbox", "maxResults": 40})
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self._proc: subprocess.Popen[str] | None = None
        self._reader: _StdoutReader | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._next_id = 0

    # -- lifecycle ----------------------------------------------------------- #

    def start(self) -> "GmailMcpClient":
        command, extra_env, cwd = _resolve_server_launch()
        env = {**os.environ, **extra_env}
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise RuntimeError(f"Could not launch Gmail MCP server ({command}): {exc}") from exc

        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._reader = _StdoutReader(self._proc.stdout)
        self._reader.start()

        self._initialize()
        return self

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - best-effort shutdown
            try:
                proc.kill()
            except OSError:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def __enter__(self) -> "GmailMcpClient":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- MCP plumbing --------------------------------------------------------- #

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr_tail.append(line.rstrip())

    def _crash_message(self, exc: Exception) -> str:
        tail = "\n".join(self._stderr_tail) or "(no stderr output captured)"
        return f"Gmail MCP server communication failed: {exc}\nServer stderr tail:\n{tail}"

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("Gmail MCP client is not running.")
        try:
            proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError(self._crash_message(exc)) from exc

    def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and return the matching result object."""
        self._next_id += 1
        request_id = self._next_id
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        self._send(request)

        reader = self._reader
        wait = self.timeout if timeout is None else timeout
        while True:
            if reader is None:
                raise RuntimeError("Gmail MCP client is not running.")
            try:
                line = reader.lines.get(timeout=wait)
            except queue.Empty as exc:
                raise RuntimeError(
                    f"Timed out after {wait}s waiting for a response to '{method}'."
                ) from exc
            if line is None:
                raise RuntimeError(self._crash_message(RuntimeError("server closed the stream")))
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue  # stray non-JSON log lines on stdout - ignore them
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue  # notifications and unrelated responses
            if "error" in message:
                raise RuntimeError(f"MCP error from '{method}': {message['error']}")
            result = message.get("result", {})
            return result if isinstance(result, dict) else {}

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _initialize(self) -> dict[str, Any]:
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        self._notify("notifications/initialized")
        return result

    # -- public API ------------------------------------------------------------ #

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the tool definitions advertised by the server."""
        return self._request("tools/list").get("tools", [])

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> str:
        """Invoke an MCP tool and return its text content."""
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout
        )
        text = _content_text(result)
        if result.get("isError"):
            raise RuntimeError(f"Gmail MCP tool '{name}' failed: {text}")
        return text


def _content_text(result: dict[str, Any]) -> str:
    """Concatenate the text blocks of an MCP tool result."""
    parts = []
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #


def _parse_search_results(text: str) -> list[dict[str, str]]:
    """
    Parse `search_emails` output into a list of message summaries.

    The server emits blocks shaped like:
        ID: <message id>
        Subject: <subject>
        From: <from header>
        Date: <date header>
    """
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    prefixes = (("ID:", "id"), ("Subject:", "subject"), ("From:", "from"), ("Date:", "date"))
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for prefix, key in prefixes:
            if line.startswith(prefix):
                value = line[len(prefix):].strip()
                if key == "id":
                    current = {"id": value, "subject": "", "from": "", "date": ""}
                    records.append(current)
                elif current is not None:
                    current[key] = value
                break
        # Lines matching no prefix are noise - skip them.
    return records


def _parse_read_email(text: str) -> dict[str, str]:
    """
    Parse `read_email` output into header fields plus the body.

    The server emits:
        Thread ID: <threadId>
        Subject: <subject>
        From: <from>
        To: <to>
        Date: <date>
        <blank line>
        <body ... attachment manifest>
    """
    headers: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    header_prefixes = (
        ("Thread ID:", "thread_id"),
        ("Subject:", "subject"),
        ("From:", "sender"),
        ("To:", "to"),
        ("Date:", "date"),
    )
    for raw_line in text.splitlines():
        if in_body:
            body_lines.append(raw_line)
            continue
        if not raw_line.strip():
            in_body = True  # blank line separates the header block from the body
            continue
        for prefix, key in header_prefixes:
            if raw_line.startswith(prefix):
                headers[key] = raw_line[len(prefix):].strip()
                break
        else:
            body_lines.append(raw_line)  # unexpected line before the blank line
    headers["body"] = "\n".join(body_lines).strip()
    return headers


_HTML_NOTE_PREFIX = "[Note: This email is HTML-formatted. Plain text version not available.]"
_HTML_LOOKALIKE_RE = re.compile(
    r"</?(?:html|body|div|span|p|table|tr|td|th|font|a|b|i|br|img|center)\b", re.IGNORECASE
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _make_snippet(body: str, max_chars: int = SNIPPET_MAX_CHARS) -> str:
    """Plain-text preview of the email body, at most `max_chars` long."""
    cut = body.find("\n\nAttachments (")  # drop the attachment manifest the server appends
    if cut != -1:
        body = body[:cut]
    if body.startswith(_HTML_NOTE_PREFIX):  # server note for HTML-only emails
        body = body[len(_HTML_NOTE_PREFIX):]
    if _HTML_LOOKALIKE_RE.search(body):  # HTML-only email: strip markup for readability
        body = _HTML_TAG_RE.sub(" ", html.unescape(body))
    collapsed = " ".join(body.split())
    if len(collapsed) > max_chars:
        collapsed = collapsed[:max_chars].rstrip() + "…"
    return collapsed


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


# Scopes the raw Google API side of this module runs with (send_reply today,
# calendar features next). The user grant is cached in token.json next to
# this file; deleting token.json and re-running triggers the browser consent
# flow for all three scopes again.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
]


def _build_gmail_service() -> Any:
    """
    Build a gmail v1 Google API service object (googleapiclient).

    Standard quickstart OAuth flow:
      * token.json (next to this file) caches the user grant between runs;
      * no token.json (or one deleted/revoked) -> the browser asks for every
        scope in GMAIL_SCOPES — gmail.readonly, gmail.send and calendar —
        through a local consent server;
      * an expired-but-refreshable cached grant is refreshed silently.

    Client secrets come from the same gcp-oauth.keys.json the Gmail MCP
    server uses (~/.gmail-mcp/, overridable via GMAIL_OAUTH_PATH), so no new
    Cloud Console setup is required. The calendar scope is requested for the
    upcoming calendar features: a calendar v3 service can be built from the
    same cached grant with build("calendar", "v3", credentials=...).

    The Google libraries are imported lazily so the fetch-only path of this
    module keeps working without them installed.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = WORKSPACE_DIR / "token.json"
    oauth_path = Path(
        os.environ.get(
            "GMAIL_OAUTH_PATH",
            str(Path.home() / ".gmail-mcp" / "gcp-oauth.keys.json"),
        )
    )

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
        except (ValueError, KeyError, OSError):
            creds = None  # corrupt/unreadable cache — fall back to fresh consent

    if not creds or not creds.valid:
        needs_consent = True
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                needs_consent = False
            except Exception:  # noqa: BLE001 — revoked/expired grant
                creds = None
        if needs_consent:
            if not oauth_path.exists():
                raise RuntimeError(
                    f"OAuth client secrets not found at {oauth_path} — set "
                    "GMAIL_OAUTH_PATH or place gcp-oauth.keys.json in "
                    "~/.gmail-mcp/."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(oauth_path), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_reply(
    thread_id: str,
    to: str,
    subject: str,
    body: str,
    message_id: str | None = None,
) -> dict[str, str]:
    """
    Send a reply email through the raw Gmail API (googleapiclient).

    Builds a MIMEText message and keeps it inside the original Gmail
    conversation by including `threadId` in the send body. When `message_id`
    is given, the In-Reply-To / References headers are set so mail clients
    recognise the message as a reply. The subject gets a 'Re: ' prefix
    unless it already starts with one (case-insensitive).

    Returns {"message_id": ..., "thread_id": ..., "status": "sent"} on success.
    """
    subject = (subject or "").strip()
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    message = MIMEText(body, "plain", "utf-8")
    message["To"] = to
    message["Subject"] = subject
    if message_id:
        message["In-Reply-To"] = message_id
        message["References"] = message_id

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    send_body: dict[str, str] = {"raw": raw, "threadId": thread_id}

    service = _build_gmail_service()
    sent: dict[str, Any] = (
        service.users()
        .messages()
        .send(userId=GMAIL_USER_ID, body=send_body)
        .execute()
    )
    return {
        "message_id": sent.get("id", ""),
        "thread_id": sent.get("threadId", thread_id),
        "status": "sent",
    }


def fetch_threads(limit: int = 20, query: str = "in:inbox") -> list[dict[str, str]]:
    """
    Fetch the most recent inbox threads: Gmail MCP server first, then the
    raw Gmail API as the deployment fallback.

    Returns up to `limit` dicts, newest first, each shaped as:
        {"thread_id": ..., "sender": ..., "subject": ..., "snippet": ..., "date": ...}

    Backend dispatch: the MCP server is preferred (identical behaviour to
    before), but when it cannot even be LAUNCHED — no Cline registration, no
    local gmail-mcp-server clone, or no node runtime, the situation on fresh
    cloud deployments — the call falls back to _fetch_threads_raw(). MCP
    failures that happen AFTER a successful launch (auth, quota, tool
    errors, timeouts) still surface unchanged: silently switching backends
    mid-session would mask the real problem.
    """
    if limit < 1:
        return []
    # Streamlit deployments do not have the local Cline/MCP process.  Allow
    # them to select the raw Gmail API explicitly, which avoids attempting to
    # launch a missing MCP binary before falling back.
    backend = os.environ.get("GMAIL_FETCH_BACKEND", "").strip().lower()
    if backend in {"raw", "gmail_api", "google_api"}:
        return _fetch_threads_raw(limit, query)
    try:
        return _fetch_threads_mcp(limit, query)
    except (FileNotFoundError, RuntimeError) as exc:
        launch_failure = (
            isinstance(exc, FileNotFoundError) or "Could not launch" in str(exc)
        )
        if not launch_failure:
            raise  # genuine MCP session error — surface it, don't switch
        return _fetch_threads_raw(limit, query)


def _decode_b64url(data: str) -> str:
    """Decode a Gmail v1 base64url body/data string to text (UTF-8, lenient)."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _payload_to_text(payload: dict) -> str:
    """
    Flatten a Gmail v1 message payload into plain text.

    Mirrors what the MCP server's read_email returns: the text/plain part
    when one exists, otherwise the text/html part with tags stripped (the
    same _HTML_TAG_RE cleanup _make_snippet applies to the MCP path's
    HTML-note bodies). Attachments and other mime types are skipped.
    """
    plain_data: str | None = None
    html_data: str | None = None
    stack = [payload]
    while stack:
        node = stack.pop(0)
        parts = node.get("parts")
        if parts:
            stack = list(parts) + stack  # BFS keeps document order
            continue
        data = (node.get("body") or {}).get("data")
        if not data:
            continue
        mime = str(node.get("mimeType", "")).lower()
        if mime == "text/plain" and plain_data is None:
            plain_data = data
        elif mime == "text/html" and html_data is None:
            html_data = data
    if plain_data:
        return _decode_b64url(plain_data)
    if html_data:
        return _HTML_TAG_RE.sub(" ", html.unescape(_decode_b64url(html_data)))
    return ""


def _fetch_threads_raw(limit: int, query: str) -> list[dict[str, str]]:
    """
    Fetch inbox threads through the raw Gmail v1 API (no MCP server).

    Fresh cloud deployments (e.g. Streamlit Community Cloud) have neither the
    Cline MCP settings file nor the gmail-mcp-server clone, so fetch falls
    back to the same googleapiclient grant (token.json) send_reply() already
    uses. Output shape is identical to the MCP path: threads come back
    newest-first and the LAST message of each thread supplies sender,
    subject, date and the body the snippet is cut from — the same fields the
    MCP path reads off the newest message.
    """
    try:
        service = _build_gmail_service()
    except RuntimeError as exc:
        if "OAuth client secrets not found" in str(exc):
            raise RuntimeError(
                "Raw Gmail fetch could not authenticate: there is no cached "
                "OAuth grant (token.json) and the browser consent flow cannot "
                "run on a headless deployment. Set the GOOGLE_TOKEN_JSON secret "
                "(the content of a local token.json) in the app's Secrets "
                "settings, or run the app locally once to create it."
            ) from exc
        raise

    listing = (
        service.users()
        .threads()
        .list(userId=GMAIL_USER_ID, q=query, maxResults=limit)
        .execute()
    )
    threads: list[dict[str, str]] = []
    for stub in listing.get("threads", [])[:limit]:
        thread = (
            service.users()
            .threads()
            .get(userId=GMAIL_USER_ID, id=stub.get("id", ""), format="full")
            .execute()
        )
        messages = thread.get("messages") or []
        if not messages:
            continue  # a thread with no messages cannot be triaged
        last = messages[-1]  # newest message — what the MCP path would read
        headers = {
            str(header.get("name", "")).lower(): str(header.get("value", ""))
            for header in (last.get("payload") or {}).get("headers", [])
        }
        threads.append(
            {
                "thread_id": thread.get("id") or stub.get("id", ""),
                "sender": headers.get("from", ""),
                "subject": headers.get("subject", ""),
                "snippet": _make_snippet(_payload_to_text(last.get("payload") or {})),
                "date": headers.get("date", ""),
            }
        )
    return threads


def _fetch_threads_mcp(limit: int, query: str) -> list[dict[str, str]]:
    """
    Fetch the most recent inbox threads through the Gmail MCP server.

    Returns up to `limit` dicts, newest first, each shaped as:
        {"thread_id": ..., "sender": ..., "subject": ..., "snippet": ..., "date": ...}

    The server exposes no listThreads tool, so threads are rebuilt from the
    newest inbox messages: twice the requested number of messages is
    requested, each is read once, and duplicates are collapsed by thread ID.
    """
    if limit < 1:
        return []

    search_buffer = max(limit * 2, limit + 10)

    threads: list[dict[str, str]] = []
    seen_thread_ids: set[str] = set()

    with GmailMcpClient() as client:
        listing = client.call_tool(
            "search_emails", {"query": query, "maxResults": search_buffer}
        )
        for message in _parse_search_results(listing):
            if len(threads) >= limit:
                break
            detail = _parse_read_email(
                client.call_tool("read_email", {"messageId": message["id"]})
            )
            thread_id = detail.get("thread_id") or message["id"]
            if thread_id in seen_thread_ids:
                continue  # another message from a thread we already captured
            seen_thread_ids.add(thread_id)
            threads.append(
                {
                    "thread_id": thread_id,
                    "sender": detail.get("sender") or message.get("from", ""),
                    "subject": detail.get("subject") or message.get("subject", ""),
                    "snippet": _make_snippet(detail.get("body", "")),
                    "date": detail.get("date") or message.get("date", ""),
                }
            )
    return threads


def run_pipeline(limit: int = 20, query: str = "in:inbox") -> list[dict[str, str]]:
    """
    Run the full Chief-of-Staff pipeline.

    Fetches the most recent inbox threads via the Gmail MCP server, classifies
    them with triage.triage_inbox (LLM backend), prints a clean priority-grouped
    digest via format_digest(), and returns the sorted results list.
    """
    inbox_threads = fetch_threads(limit, query)
    results = triage_inbox(inbox_threads)
    format_digest(results)
    return results


DIGEST_WIDTH = 70  # width of the digest header rules and group separators

# Canonical priority order, mirroring the sort applied by triage.py.
PRIORITY_ORDER = ["urgent", "needs-reply", "important", "fyi", "low-priority", "spam"]


def _ensure_utf8_output() -> None:
    """Windows consoles/redirects default to a legacy codepage (e.g. cp1252)
    while snippets and classifications can contain arbitrary Unicode."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def format_digest(results: list[dict[str, str]]) -> None:
    """
    Print a clean, readable digest of the triaged, priority-sorted results.

    Header shows 'INBOX DIGEST', today's date and the total thread count.
    Each line:  [PRIORITY] sender | subject — reason
    Threads sharing a priority form one group; a separator line is printed
    between consecutive priority groups.
    """
    _ensure_utf8_output()

    today = date.today()
    header_date = f"{today.strftime('%A, %B')} {today.day}, {today.year}"
    rule = "=" * DIGEST_WIDTH

    print(rule)
    print(f"INBOX DIGEST — {header_date}")
    print(f"Total threads: {len(results)}")
    print(rule)

    if not results:
        print("No threads to show.")
        print()
        return

    # Group by priority: canonical order first, unexpected values last.
    groups: dict[str, list[dict[str, str]]] = {}
    for item in results:
        groups.setdefault(item.get("priority", "unknown"), []).append(item)
    ordered = [p for p in PRIORITY_ORDER if p in groups]
    ordered += sorted(p for p in groups if p not in PRIORITY_ORDER)

    first_group = True
    for priority in ordered:
        if not first_group:
            print("-" * DIGEST_WIDTH)  # separator line between priority groups
        first_group = False
        for item in groups[priority]:
            print(
                f"[{item['priority'].upper()}] {item['sender']} | "
                f"{item['subject']} — {item['reason']}"
            )
    print()


if __name__ == "__main__":
    # Prints the INBOX DIGEST, then the returned results list.
    print(run_pipeline(2))
