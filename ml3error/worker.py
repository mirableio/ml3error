from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from . import constants
from .store.base import Store
from .transport import Transport

log = logging.getLogger("ml3error")

_SENTINEL: Any = object()


class Worker:
    """Single daemon thread draining a bounded queue of rendered payloads.

    Not thread-safe in construction; the orchestrator creates one Worker
    per Reporter lifecycle. `submit()` is safe to call from any thread
    provided the caller holds the orchestrator's lock (so that the
    in-flight set and queue stay in sync on overflow).
    """

    def __init__(
        self,
        store: Store,
        transport: Transport,
        lock: threading.Lock,
        in_flight: set[str],
    ):
        self._store = store
        self._transport = transport
        self._lock = lock
        self._in_flight = in_flight
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=constants.QUEUE_SIZE)
        self._thread: threading.Thread | None = None

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            t = threading.Thread(
                target=self._run, daemon=True, name="ml3error-worker"
            )
            t.start()
            self._thread = t

    def submit(self, fp_key: str, subject: str, body: str, reported_count: int) -> bool:
        """Caller MUST hold the orchestrator's lock and MUST have already
        added `fp_key` to the in-flight set. Returns False if the queue
        was full — the caller is then responsible for rolling back the
        in-flight entry and bumping the dropped counter.

        `reported_count` is the suppressed_count value rendered into the
        payload; it's passed back to record_sent so that bumps happening
        after submit (same fp occurring while worker is mid-send) are
        preserved rather than clobbered.
        """
        try:
            self._queue.put_nowait((fp_key, subject, body, reported_count))
        except queue.Full:
            return False
        self._ensure_thread()
        return True

    def _run(self) -> None:
        # Import here to avoid a circular import at module load.
        from . import _LockWithGuard

        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                self._queue.task_done()
                return
            fp_key, subject, body, reported_count = item
            try:
                ok = self._transport.send(subject, body)
            except Exception:
                log.warning("transport.send raised for %s", fp_key, exc_info=True)
                ok = False
            now = time.time()
            # _LockWithGuard sets the thread-local reentrance flag so
            # any log.warning below (under the lock) can't flow back
            # through LoggingHandler → report() → lock-reacquire and
            # wedge this worker thread.
            with _LockWithGuard(self._lock):
                try:
                    if ok:
                        self._store.record_sent(fp_key, now, reported_count)
                    else:
                        self._store.bump_transport_failures()
                except Exception:
                    log.warning(
                        "post-send store update failed for %s", fp_key, exc_info=True
                    )
                self._in_flight.discard(fp_key)
            self._queue.task_done()

    def shutdown(self, drain_timeout: float = constants.ATEXIT_DRAIN_SECONDS) -> None:
        if self._thread is None or not self._thread.is_alive():
            return
        try:
            self._queue.put(_SENTINEL, timeout=drain_timeout)
        except queue.Full:
            # Give up on draining; process is exiting anyway.
            return
        self._thread.join(drain_timeout)
