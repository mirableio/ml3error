"""Minimal web UI to review fingerprints and mark them resolved.

Stdlib-only (http.server). Static assets live in ml3error/static/.
"""
from __future__ import annotations

import http.server
import json
import mimetypes
import socketserver
import threading
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .store import open_store
from .store.base import Store

_STATIC_DIR = Path(__file__).parent / "static"


def _json(handler: http.server.BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _serve_file(handler: http.server.BaseHTTPRequestHandler, path: Path) -> None:
    try:
        body = path.read_bytes()
    except FileNotFoundError:
        handler.send_error(404, "not found")
        return
    ctype, _ = mimetypes.guess_type(str(path))
    if not ctype:
        ctype = "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    # Disable caching so edits to index.html / script.js show up on reload.
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _safe_static(rel: str) -> Path | None:
    """Resolve a request path like '/static/script.js' to a file under
    _STATIC_DIR, rejecting anything that escapes the directory."""
    candidate = (_STATIC_DIR / rel).resolve()
    try:
        candidate.relative_to(_STATIC_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


class _Handler(http.server.BaseHTTPRequestHandler):
    # Injected by start_server.
    store: Store
    lock: threading.Lock

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # Silence default stdout access logs.

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/":
            _serve_file(self, _STATIC_DIR / "index.html")
            return
        if url.path.startswith("/static/"):
            rel = url.path[len("/static/"):]
            resolved = _safe_static(rel)
            if resolved is None:
                self.send_error(404, "not found")
                return
            _serve_file(self, resolved)
            return
        if url.path == "/api/fingerprints":
            q = parse_qs(url.query)
            resolved_q = q.get("resolved", [None])[0]
            resolved: bool | None
            if resolved_q == "1":
                resolved = True
            elif resolved_q == "0":
                resolved = False
            else:
                resolved = None
            with self.lock:
                items = self.store.list_fingerprints(resolved=resolved)
            _json(self, 200, {"items": items})
            return
        self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        # /api/fingerprints/<fp>/resolved
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "fingerprints" and parts[3] == "resolved":
            fp = parts[2]
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json(self, 400, {"error": "invalid json"})
            resolved = bool(payload.get("resolved", True))
            with self.lock:
                self.store.set_resolved(fp, resolved)
            return _json(self, 200, {"ok": True, "fp": fp, "resolved": resolved})
        self.send_error(404, "not found")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(
    state_url: str,
    host: str = "127.0.0.1",
    port: int = 6025,
    open_browser: bool = True,
) -> None:
    """Open the given state URL read/write and serve the review UI."""
    store, crash_store = open_store(state_url)
    crash_store.close()  # not needed for the UI
    lock = threading.Lock()

    handler_cls = type(
        "_BoundHandler",
        (_Handler,),
        {"store": store, "lock": lock},
    )
    server = _Server((host, port), handler_cls)
    url = f"http://{host}:{port}"
    print(f"ml3error web UI at {url} (state: {state_url})")
    print("Press Ctrl+C to stop.")
    if open_browser:
        # Fire after a tiny delay so the server is definitely accepting
        # connections by the time the browser sends the first request.
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


def _default_state_url() -> str:
    """Fallback state URL when --state isn't passed and env isn't set."""
    return f"sqlite:///{Path.cwd() / '.ml3error.db'}"
