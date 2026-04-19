from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request

from ml3error import web
from ml3error.store.sqlite import SQLiteStore


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(store: SQLiteStore, port: int):
    """Spin up a _Server directly (not serve(), which blocks)."""
    lock = threading.Lock()
    handler_cls = type("_BoundHandler", (web._Handler,), {"store": store, "lock": lock})
    server = web._Server(("127.0.0.1", port), handler_cls)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Wait for the socket to be ready.
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.02)
    return server


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as resp:
        return json.loads(resp.read())


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        return json.loads(resp.read())


def test_web_lists_and_resolves(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    store.decide("fpA", ("ValueError", "a.py", "fa"), 60, 1000.0)
    store.decide("fpB", ("TypeError", "b.py", "fb"), 60, 1100.0)

    port = _free_port()
    server = _start_server(store, port)
    try:
        base = f"http://127.0.0.1:{port}"

        # Root serves HTML.
        with urllib.request.urlopen(base + "/", timeout=2) as resp:
            html = resp.read().decode()
            assert "<title>ml3error</title>" in html

        # List all → both present.
        all_data = _get_json(base + "/api/fingerprints")
        fps = [it["fp"] for it in all_data["items"]]
        assert "fpA" in fps and "fpB" in fps

        # List unresolved → still both.
        un = _get_json(base + "/api/fingerprints?resolved=0")
        assert len(un["items"]) == 2

        # Resolve fpA via API.
        r = _post_json(base + f"/api/fingerprints/fpA/resolved", {"resolved": True})
        assert r["ok"] is True

        # Now unresolved list has one; resolved list has the other.
        un = _get_json(base + "/api/fingerprints?resolved=0")
        res = _get_json(base + "/api/fingerprints?resolved=1")
        assert [it["fp"] for it in un["items"]] == ["fpB"]
        assert [it["fp"] for it in res["items"]] == ["fpA"]
    finally:
        server.shutdown()
        server.server_close()
        store.close()
