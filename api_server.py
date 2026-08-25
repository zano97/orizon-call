"""
Local HTTP API server for web app integration.

Security model
--------------
* Bound to 127.0.0.1 only (never reachable from LAN). The Host header is
  validated on every request, so DNS-rebinding pages cannot reach the
  API even when auth is disabled.
* Every protected endpoint requires `Authorization: Bearer <token>` where
  <token> is a random 256-bit value generated at startup and written to
  ~/.orizon-call/token (created with mode 0600). The companion web app
  reads that file (same machine) and includes it in every request.
  GET /events also accepts `?token=<token>` because the browser
  EventSource API cannot send custom headers.
* CORS: by default only loopback Origins are allowed. A specific origin
  can be added via --cors-origin (e.g. https://app.orizon.com). Wildcard
  CORS is not used: combined with the localhost listener + a hijackable
  browser session it would be a CSRF + exfiltration vector.

Endpoints
---------
Unprotected (no auth, useful for the web app to discover the server):
  GET  /health        → {"ok": true}
  OPTIONS *           → CORS preflight

Protected (require token):
  GET  /status        → snapshot of recorder state
  GET  /events        → SSE stream of status updates (header or ?token=)
  GET  /files         → list of recordings in the output dir
  GET  /files/<name>  → download a single recording
  POST /start         → begin recording (409 unless idle)
  POST /stop          → stop recording (409 unless recording/paused)
  POST /pause         → pause
  POST /resume        → resume
  POST /mute          → mute mic
  POST /unmute        → unmute mic
  POST /quit          → exit the app
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import stat
import time
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import TYPE_CHECKING, Optional

from orizon_logging import get_logger

if TYPE_CHECKING:
    from floating_widget import FloatingRecorderWidget

log = get_logger("api")

MAX_SSE_CONNECTIONS = 16


# ---------- Auth token ----------

def _token_path() -> Path:
    base = Path.home() / ".orizon-call"
    base.mkdir(parents=True, exist_ok=True)
    return base / "token"


def issue_token() -> str:
    """Generate a fresh token and persist it. The file is created with
    mode 0600 from the start — no window where it is world-readable."""
    token = secrets.token_urlsafe(32)
    path = _token_path()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # pre-existing files too
    except OSError:
        # Windows: chmod is a no-op; ACLs do the work.
        pass
    return token


def _token_matches(candidate: str, expected: str) -> bool:
    try:
        return secrets.compare_digest(
            candidate.encode("utf-8", errors="replace"),
            expected.encode("utf-8"),
        )
    except Exception:
        return False


# ---------- Server ----------

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class RecorderAPIHandler(BaseHTTPRequestHandler):

    widget: "Optional[FloatingRecorderWidget]" = None
    output_dir: Optional[Path] = None
    auth_token: Optional[str] = None  # None = auth disabled
    cors_allowed_origin: Optional[str] = None  # extra origin beyond loopback

    _sse_clients = 0
    _sse_lock = threading.Lock()

    # Endpoints that do NOT require auth.
    PUBLIC_PATHS = {"/health"}

    _TOKEN_RE = re.compile(r"(token=)[^&\s\"]+")

    def log_message(self, format, *args):
        # Suppress default stderr access log; the logger is enough.
        # Redact query-string tokens (?token=... on /events) — the debug
        # log must never persist the bearer secret.
        message = format % args
        log.debug(self._TOKEN_RE.sub(r"\1<redacted>", message))

    # ---------- Host validation (DNS rebinding) ----------

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return True  # HTTP/1.0 probes (our own single-instance check)
        # Strip port; tolerate bracketed IPv6.
        if host.startswith("["):
            hostname = host.partition("]")[0].lstrip("[")
        else:
            hostname = host.rsplit(":", 1)[0] if ":" in host else host
        return hostname.lower() in ("localhost", "127.0.0.1", "::1")

    # ---------- CORS ----------

    def _origin_allowed(self, origin: Optional[str]) -> bool:
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlparse(origin)
        except ValueError:
            return False
        host = parsed.hostname or ""
        if host in ("localhost", "127.0.0.1", "::1"):
            return True
        if RecorderAPIHandler.cors_allowed_origin and origin == RecorderAPIHandler.cors_allowed_origin:
            return True
        return False

    def _set_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json_response(self, status: int, data: dict) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._set_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):  # noqa: N802 — http.server API
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    # ---------- Auth gate ----------

    def _authorize(self, path: str, query: dict) -> bool:
        if not self._host_allowed():
            self._json_response(403, {"error": "invalid host"})
            return False
        if path in self.PUBLIC_PATHS or RecorderAPIHandler.auth_token is None:
            return True
        header = self.headers.get("Authorization", "")
        token = ""
        if header.startswith("Bearer "):
            token = header[len("Bearer "):].strip()
        elif path == "/events":
            # EventSource cannot send custom headers — allow ?token= for
            # the SSE endpoint only.
            values = query.get("token") or []
            token = values[0] if values else ""
        if not token:
            self._json_response(401, {"error": "missing bearer token"})
            return False
        if not _token_matches(token, RecorderAPIHandler.auth_token):
            self._json_response(403, {"error": "invalid token"})
            return False
        return True

    # ---------- Routing ----------

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authorize(path, query):
            return

        if path == "/status":
            self._handle_status()
        elif path == "/health":
            self._json_response(200, {"ok": True})
        elif path == "/events":
            self._handle_events_sse()
        elif path == "/files":
            self._handle_files_list()
        elif path.startswith("/files/"):
            filename = urllib.parse.unquote(path[len("/files/"):])
            self._handle_file_download(filename)
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        self._drain_request_body()
        if not self._authorize(path, urllib.parse.parse_qs(parsed.query)):
            return

        if path == "/start":
            self._invoke_widget_slot("api_start", started="starting", require_state="idle")
        elif path == "/stop":
            self._handle_stop_with_meta()
        elif path == "/pause":
            self._invoke_widget_slot("api_pause", started="pausing", require_state="recording")
        elif path == "/resume":
            self._invoke_widget_slot("api_resume", started="resuming", require_state="paused")
        elif path == "/mute":
            self._invoke_widget_slot("api_mute", started="muted")
        elif path == "/unmute":
            self._invoke_widget_slot("api_unmute", started="unmuted")
        elif path == "/quit":
            self._invoke_widget_slot("api_quit", started="quitting")
        else:
            self._json_response(404, {"error": "not found"})

    def _drain_request_body(self) -> None:
        """Read and discard any request body so the response isn't sent
        while unread data sits in the socket buffer."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        remaining = min(length, 1 << 20)  # never read more than 1 MB
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    # ---------- Slot invocation helpers ----------

    def _widget(self) -> "Optional[FloatingRecorderWidget]":
        return RecorderAPIHandler.widget

    def _invoke_widget_slot(
        self,
        slot_name: str,
        started: str,
        require_state: Optional[str] = None,
    ) -> None:
        w = self._widget()
        if not w:
            self._json_response(503, {"error": "recorder not ready"})
            return
        if require_state is not None and w.recorder_state_name() != require_state:
            self._json_response(409, {"error": f"requires state {require_state}",
                                       "state": w.recorder_state_name()})
            return
        from PyQt6.QtCore import QMetaObject, Qt as QtConst
        QMetaObject.invokeMethod(w, slot_name, QtConst.ConnectionType.QueuedConnection)
        self._json_response(200, {"status": started})

    def _handle_stop_with_meta(self) -> None:
        w = self._widget()
        if not w:
            self._json_response(503, {"error": "recorder not ready"})
            return
        if w.recorder_state_name() not in ("recording", "paused"):
            self._json_response(409, {"error": "not recording",
                                       "state": w.recorder_state_name()})
            return
        status = w.recorder_status()
        from PyQt6.QtCore import QMetaObject, Qt as QtConst
        QMetaObject.invokeMethod(w, "api_stop", QtConst.ConnectionType.QueuedConnection)
        self._json_response(200, {
            "status": "stopping",
            "elapsed": status["elapsed"],
            "output_path": status["output_path"],
        })

    # ---------- Status ----------

    def _handle_status(self) -> None:
        w = self._widget()
        if not w:
            self._json_response(503, {"error": "recorder not ready"})
            return
        self._json_response(200, w.recorder_status())

    # ---------- SSE Events ----------

    def _handle_events_sse(self) -> None:
        w = self._widget()
        if not w:
            self._json_response(503, {"error": "recorder not ready"})
            return

        with RecorderAPIHandler._sse_lock:
            if RecorderAPIHandler._sse_clients >= MAX_SSE_CONNECTIONS:
                self._json_response(503, {"error": "too many event streams"})
                return
            RecorderAPIHandler._sse_clients += 1

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._set_cors_headers()
            self.end_headers()

            last_state: Optional[str] = None
            while True:
                try:
                    status = w.recorder_status()
                    current_state = status.get("state")
                    data_line = json.dumps(status, default=str)
                    self.wfile.write(f"data: {data_line}\n\n".encode())
                    if current_state != last_state and last_state is not None:
                        self.wfile.write(f"event: statechange\ndata: {data_line}\n\n".encode())
                    last_state = current_state
                    self.wfile.flush()
                    w.wait_for_status_change(timeout=1.0)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            with RecorderAPIHandler._sse_lock:
                RecorderAPIHandler._sse_clients -= 1

    # ---------- Files ----------

    def _get_recordings_dir(self) -> Path:
        if RecorderAPIHandler.output_dir:
            return RecorderAPIHandler.output_dir
        return Path.home() / "Downloads"

    def _handle_files_list(self) -> None:
        rec_dir = self._get_recordings_dir()
        files = []
        for ext in ("*.wav", "*.flac", "*.mp3"):
            for f in rec_dir.glob(f"recording_{ext}"):
                try:
                    s = f.stat()
                except OSError:
                    continue
                files.append({
                    "name": f.name,
                    "size": s.st_size,
                    "modified": s.st_mtime,
                    "path": str(f),
                })
        # Most recent first across ALL extensions, then cap.
        files.sort(key=lambda item: item["modified"], reverse=True)
        self._json_response(200, {"files": files[:50]})

    def _handle_file_download(self, filename: str) -> None:
        if "/" in filename or "\\" in filename or ".." in filename or "\x00" in filename:
            self._json_response(400, {"error": "invalid filename"})
            return
        # Access policy first: don't leak existence of non-recording files.
        if not filename.startswith("recording_"):
            self._json_response(403, {"error": "access denied"})
            return
        rec_dir = self._get_recordings_dir()
        filepath = rec_dir / filename
        if not filepath.exists() or not filepath.is_file():
            self._json_response(404, {"error": "file not found"})
            return

        ext = filepath.suffix.lower()
        content_types = {".wav": "audio/wav", ".flac": "audio/flac", ".mp3": "audio/mpeg"}
        content_type = content_types.get(ext, "application/octet-stream")

        file_size = filepath.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(file_size))
        self._set_cors_headers()
        self.end_headers()

        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break


def start_api_server(
    widget: "FloatingRecorderWidget",
    port: int = 19876,
    output_dir: Optional[Path] = None,
    cors_origin: Optional[str] = None,
    require_auth: bool = True,
    bound_socket: Optional[socket.socket] = None,
) -> Optional[ThreadedHTTPServer]:
    """Start the threaded API server. Returns the server or None on failure.

    When ``bound_socket`` is provided (the single-instance probe socket),
    the server adopts it instead of binding again — no close/rebind race.
    """
    RecorderAPIHandler.widget = widget
    RecorderAPIHandler.output_dir = output_dir
    RecorderAPIHandler.cors_allowed_origin = cors_origin
    if require_auth:
        RecorderAPIHandler.auth_token = issue_token()
        # The token itself must never hit the logs.
        log.info("Auth token written to %s", _token_path())
    else:
        RecorderAPIHandler.auth_token = None
        log.warning("API auth DISABLED (--no-auth). Anyone with browser access "
                    "to localhost can control the recorder.")

    try:
        if bound_socket is not None:
            server = ThreadedHTTPServer(
                ("127.0.0.1", port), RecorderAPIHandler, bind_and_activate=False)
            server.socket = bound_socket
            server.server_address = bound_socket.getsockname()
            server.server_activate()
        else:
            server = ThreadedHTTPServer(("127.0.0.1", port), RecorderAPIHandler)
    except OSError as e:
        log.error("Cannot bind to port %d: %s", port, e)
        return None

    thread = threading.Thread(target=server.serve_forever, daemon=True, name="api-server")
    thread.start()
    log.info("API listening on http://127.0.0.1:%d", port)
    return server
