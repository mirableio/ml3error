from __future__ import annotations

import threading
import time

from ml3error.heartbeat import Heartbeat, _format_summary, _today_slot_timestamp
from ml3error.store.sqlite import SQLiteStore
from ml3error.config import Config
from datetime import time as dt_time


class _FakeTransport:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, subject: str, body: str) -> bool:
        self.sent.append((subject, body))
        return True


FP = ("ValueError", "app.py", "main")


def test_format_summary_lists_active_errors(tmp_path):
    now = 1_000_000.0
    since = now - 3600
    fps = [
        {
            "exc_type": "ValueError", "rel_path": "app.py", "func_name": "main",
            "total_count": 5, "last_activity": now - 60, "resolved": False,
        },
        {
            "exc_type": "OldError", "rel_path": "legacy.py", "func_name": "x",
            "total_count": 50, "last_activity": since - 10, "resolved": False,  # out of window
        },
        {
            "exc_type": "DoneError", "rel_path": "fixed.py", "func_name": "y",
            "total_count": 3, "last_activity": now - 30, "resolved": True,  # resolved
        },
    ]
    subj, body = _format_summary("proj", now, since, fps, dropped=0, fails=0, suppressed=2)
    assert "ValueError" in body
    assert "OldError" not in body  # filtered by window
    assert "DoneError" not in body  # filtered by resolved
    assert "1 active error" in subj
    # Active-errors variant uses the alert emoji.
    assert "🚨" in subj
    # Per-row lifetime total is still shown in the body.
    assert "5× total" in body
    assert "Library:" not in body  # no transport/dropped errors → skipped


def test_format_summary_surfaces_library_stats_when_nonzero():
    now = 1_000_000.0
    subj, body = _format_summary("proj", now, now - 3600, [], dropped=3, fails=1, suppressed=0)
    assert "Library: 1 transport failure(s), 3 dropped" in body


def test_format_summary_uses_check_emoji_when_no_errors():
    now = 1_000_000.0
    subj, body = _format_summary("proj", now, now - 3600, [], dropped=0, fails=0, suppressed=0)
    assert "✅" in subj
    assert "0 active errors" in subj
    assert "No errors active in this window" in body


def test_preview_does_not_touch_state(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    now = time.time()
    store.decide("fp1", FP, 60, now)
    store.bump_dropped()
    store.bump_dropped()

    # snapshot state before preview
    before_counters = store.read_counters()
    before_last_heartbeat = store.get_last_heartbeat()

    transport = _FakeTransport()
    hb = Heartbeat("proj", dt_time(9, 0), store, transport, threading.Lock())
    assert hb.fire(update_state=False) is True
    assert len(transport.sent) == 1

    # Preview (update_state=False) must not touch last_heartbeat or counters.
    assert store.read_counters() == before_counters
    assert store.get_last_heartbeat() == before_last_heartbeat


def test_fire_advances_state(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    now = time.time()
    store.decide("fp1", FP, 60, now)
    store.bump_dropped()

    transport = _FakeTransport()
    hb = Heartbeat("proj", dt_time(9, 0), store, transport, threading.Lock())
    hb.fire(now=now)

    # Counters reduced to zero (snapshot subtracted) and last_heartbeat set.
    assert store.read_counters() == (0, 0, 0)
    assert store.get_last_heartbeat() is not None


def test_fire_claims_slot_once_across_shared_store(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    target = dt_time(9, 0)
    now = _today_slot_timestamp(target, time.time()) + 60

    t1 = _FakeTransport()
    t2 = _FakeTransport()
    hb1 = Heartbeat("proj", target, store, t1, threading.Lock())
    hb2 = Heartbeat("proj", target, store, t2, threading.Lock())

    assert hb1.fire(now=now) is True
    assert hb2.fire(now=now) is False
    assert len(t1.sent) + len(t2.sent) == 1


def test_fire_uses_previous_heartbeat_window_after_claim(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    target = dt_time(9, 0)
    slot = _today_slot_timestamp(target, time.time())
    previous_slot = slot - 86400
    store.set_last_heartbeat(previous_slot)
    store.decide("fp1", FP, 60, previous_slot + 100)

    transport = _FakeTransport()
    hb = Heartbeat("proj", target, store, transport, threading.Lock())

    assert hb.fire(now=slot + 60) is True
    assert len(transport.sent) == 1
    assert "ValueError" in transport.sent[0][1]
