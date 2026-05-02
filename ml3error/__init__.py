"""ml3error — lightweight error notifier for hobby Python projects.

Public API:
    init(**kwargs)
    report(exc, locals=None)
    ping()
    close()
    LoggingHandler
"""

from __future__ import annotations

import atexit
import logging
import sys
import threading
import time
from types import TracebackType
from typing import Any, Callable

from . import config as _config
from . import constants, fingerprint, hooks
from . import transport as _transport_mod

# Library-scoped logger. Users get diagnostics by configuring their
# own handler on "ml3error" (or the root logger). We never propagate
# library errors via raise — this is the visibility channel.
log = logging.getLogger("ml3error")

# Thread-local reentrance guard. LoggingHandler.emit() checks this and
# bails out when truthy, so any records that fire while we're inside a
# reporter critical section (holding r.lock) can't be fed back into
# report() and deadlock on re-acquiring the lock. Set by both the
# caller path (_enqueue) and the worker thread (post-send writebacks).
_in_critical = threading.local()


def _is_in_critical() -> bool:
    return getattr(_in_critical, "value", False)


class _LockWithGuard:
    """Context manager: acquire a lock and set _in_critical on the
    current thread for its duration. Pair with LoggingHandler's guard
    to prevent same-thread reentry via the logging pipeline.
    """

    def __init__(self, lock: threading.Lock):
        self._lock = lock

    def __enter__(self) -> None:
        self._lock.acquire()
        _in_critical.value = True

    def __exit__(self, exc_type, exc, tb) -> None:
        _in_critical.value = False
        self._lock.release()
from .constants import (
    CRASH_TIMEOUT,
    HTTP_TIMEOUT,
    MAX_MESSAGE_CHARS,
    QUEUE_SIZE,
    SCRUB_PATTERNS,
)
from .heartbeat import Heartbeat
from .logging_handler import LoggingHandler
from .store import open_store
from .store.base import CrashStore, Decision, Store
from .worker import Worker

__all__ = [
    "init",
    "report",
    "ping",
    "close",
    "install_asyncio_hook",
    "LoggingHandler",
    "QUEUE_SIZE",
    "HTTP_TIMEOUT",
    "CRASH_TIMEOUT",
    "MAX_MESSAGE_CHARS",
    "SCRUB_PATTERNS",
]


class _Reporter:
    def __init__(
        self,
        cfg: _config.Config,
        store: Store,
        crash_store: CrashStore,
        transport: _transport_mod.Transport,
    ):
        self.cfg = cfg
        self.store = store
        self.crash_store = crash_store
        self.transport = transport
        self.lock = threading.Lock()
        self.in_flight: set[str] = set()
        self.worker = Worker(store, transport, self.lock, self.in_flight)
        self.heartbeat: Heartbeat | None = None
        self.uninstallers: list[Callable[[], None]] = []
        self.closed = False


_reporter: _Reporter | None = None
_init_lock = threading.Lock()
_atexit_registered = False


def _require_reporter() -> _Reporter:
    r = _reporter
    if r is None:
        raise RuntimeError("ml3error.init() has not been called")
    if r.closed:
        raise RuntimeError("ml3error has been closed; call init() to resume")
    return r


def init(**kwargs: Any) -> None:
    """Initialize ml3error. Idempotent with the same arguments.

    Raises RuntimeError if called a second time with different arguments
    (call close() first to reconfigure).
    """
    global _reporter, _atexit_registered

    cfg = _config.build(**kwargs)

    with _init_lock:
        if _reporter is not None and not _reporter.closed:
            if _reporter.cfg == cfg:
                return
            raise RuntimeError(
                "ml3error.init() already called with different arguments; "
                "call close() first to reconfigure."
            )

        store, crash_store = open_store(cfg.state)
        try:
            transport = _transport_mod.Transport(cfg.transport, cfg.transport_config)
        except Exception:
            store.close()
            crash_store.close()
            raise

        r = _Reporter(cfg, store, crash_store, transport)

        for hook_name in cfg.hooks:
            if hook_name == "excepthook":
                r.uninstallers.append(hooks.install_excepthook(_crash_handler))
            elif hook_name == "threading":
                r.uninstallers.append(hooks.install_threading_hook(_threading_handler))
            elif hook_name == "asyncio":
                r.uninstallers.append(hooks.install_asyncio_hook(_asyncio_handler))

        if cfg.heartbeat:
            r.heartbeat = Heartbeat(
                cfg.project, cfg.heartbeat_time, store, transport, r.lock
            )
            r.heartbeat.start()

        _reporter = r

        if not _atexit_registered:
            atexit.register(_atexit_close)
            _atexit_registered = True


def close() -> None:
    """Flush the queue, stop background threads, release resources."""
    with _init_lock:
        r = _reporter
        if r is None or r.closed:
            return
        r.closed = True

        for fn in r.uninstallers:
            try:
                fn()
            except Exception:
                pass
        r.uninstallers.clear()

        if r.heartbeat is not None:
            try:
                r.heartbeat.stop()
            except Exception:
                pass

        try:
            r.worker.shutdown(constants.ATEXIT_DRAIN_SECONDS)
        except Exception:
            pass

        # Only close the stores if the worker actually stopped. If a
        # slow transport outlives the drain timeout, the worker thread
        # is still running and will try to record_sent / bump counters
        # when its send finishes; closing the SQLite connection now
        # would make those writes fail. On process exit the daemon
        # thread and the connections are torn down by the interpreter
        # anyway, so leaking them here is safe.
        worker_thread = r.worker._thread  # type: ignore[attr-defined]
        if worker_thread is None or not worker_thread.is_alive():
            try:
                r.store.close()
            except Exception:
                pass
            try:
                r.crash_store.close()
            except Exception:
                pass
        else:
            log.warning(
                "close(): worker still running after drain timeout; "
                "leaving state store open so pending writebacks don't fail",
            )


def _atexit_close() -> None:
    try:
        close()
    except Exception:
        pass


def ping() -> None:
    """Send a synthetic message to verify the transport works.

    Bypasses queue, cooldown, and scrubbing. Raises if the send fails.
    """
    r = _require_reporter()
    subject = f"[{r.cfg.project}] ml3error ping"
    body = f"ml3error ping from {r.cfg.project}"
    ok, reason = r.transport.send_detailed(subject, body)
    if not ok:
        raise RuntimeError(
            f"ml3error ping failed via transport {r.cfg.transport!r}: {reason}"
        )


def install_asyncio_hook() -> None:
    """Install the asyncio exception handler against the current event loop.

    Must be called after init() and from inside a coroutine (or after the
    loop has started), since asyncio's `get_running_loop()` is the only
    reliable way to locate the loop we want to hook.

    Use this when you can't include `"asyncio"` in init's `hooks=` list —
    most commonly because init() runs at import time, before the event
    loop exists. Call this once from inside your async entry point:

        async def main():
            ml3error.install_asyncio_hook()
            ...

    The installed handler is chained with whatever was set before, and
    is removed by close() along with the other hooks.
    """
    r = _require_reporter()
    uninstaller = hooks.install_asyncio_hook(_asyncio_handler)
    r.uninstallers.append(uninstaller)


def report(exc: BaseException, locals: bool | None = None) -> None:
    """Report an exception. Thread-safe. Honors cooldown and in-flight dedup.

    `locals` overrides the init-level `with_locals` default for this call only.
    Pass False at sites handling sensitive values even if locals capture is
    enabled globally.
    """
    r = _require_reporter()
    tb = _resolve_traceback(exc)
    with_locals = r.cfg.with_locals if locals is None else bool(locals)
    fp = fingerprint.compute(exc, tb, r.cfg.project_root)

    def render(decision: Decision) -> tuple[str, str]:
        payload = _transport_mod.build_payload(
            project=r.cfg.project,
            fp=fp,
            exc=exc,
            tb=tb,
            first_seen=decision.first_seen,
            suppressed_count=decision.suppressed_count,
            with_locals=with_locals,
            regression=decision.was_resolved,
        )
        return _transport_mod.render(
            payload,
            cooldown_hours=r.cfg.cooldown_hours,
            markup=r.transport.markup,
        )

    _enqueue(fp, render)


def _report_log_record(record: "logging.LogRecord") -> None:  # noqa: F821
    """Internal: report a logging.LogRecord that has no exc_info.

    Used by LoggingHandler so bare `log.error("db down")` calls still
    generate notifications, fingerprinted by (level, pathname, funcName).
    """
    r = _require_reporter()
    fp = fingerprint.compute_from_record(record, r.cfg.project_root)

    def render(decision: Decision) -> tuple[str, str]:
        payload = _transport_mod.build_log_payload(
            project=r.cfg.project,
            fp=fp,
            record=record,
            first_seen=decision.first_seen,
            suppressed_count=decision.suppressed_count,
            regression=decision.was_resolved,
        )
        return _transport_mod.render(
            payload,
            cooldown_hours=r.cfg.cooldown_hours,
            markup=r.transport.markup,
        )

    _enqueue(fp, render)


def _enqueue(
    fp: tuple[str, str, str],
    render: Callable[[Decision], tuple[str, str]],
) -> None:
    """Shared lock → decide → render → enqueue flow for both report()
    and _report_log_record(). The render callback builds (subject, body)
    from the decision; everything else is identical."""
    r = _require_reporter()
    fp_key = fingerprint.to_key(fp)
    now = time.time()
    cooldown_seconds = r.cfg.cooldown_hours * 3600.0

    # Hold the lock for the entire check → decide → enqueue sequence.
    # Three invariants must hold together, which is why one critical
    # section covers them all:
    #   1. An fp that is in-flight never enqueues a duplicate.
    #   2. Cooldown status read and suppressed_count bump are atomic.
    #   3. If decide says "send", we add to in_flight before releasing
    #      the lock, so a concurrent caller sees us as in-flight.
    with _LockWithGuard(r.lock):
        if fp_key in r.in_flight:
            try:
                r.store.bump_suppressed(fp_key)
            except Exception:
                log.warning("store.bump_suppressed failed for %s", fp_key, exc_info=True)
            return

        try:
            decision = r.store.decide(fp_key, fp, cooldown_seconds, now)
        except Exception:
            # Store is unavailable (e.g. Redis connection just died).
            # Prefer "maybe duplicate" over "definitely miss the alert".
            log.warning(
                "store.decide failed; sending without dedup state",
                exc_info=True,
            )
            decision = Decision(should_send=True, first_seen=now, suppressed_count=0)
        if not decision.should_send:
            return

        subject, body = render(decision)

        try:
            r.store.set_last_message(fp_key, body)
        except Exception:
            log.warning("store.set_last_message failed for %s", fp_key, exc_info=True)

        r.in_flight.add(fp_key)
        if not r.worker.submit(fp_key, subject, body, decision.suppressed_count):
            r.in_flight.discard(fp_key)
            try:
                r.store.bump_dropped()
            except Exception:
                log.warning("store.bump_dropped failed", exc_info=True)


def _resolve_traceback(exc: BaseException) -> TracebackType | None:
    if exc.__traceback__ is not None:
        return exc.__traceback__
    info = sys.exc_info()
    if info[1] is exc:
        return info[2]
    return None


def _crash_handler(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
) -> None:
    """sys.excepthook path: inline send with CRASH_TIMEOUT, lockless store."""
    r = _reporter
    if r is None or r.closed:
        return
    try:
        fp = fingerprint.compute(exc, tb, r.cfg.project_root)
        fp_key = fingerprint.to_key(fp)
        now = time.time()
        cooldown_seconds = r.cfg.cooldown_hours * 3600.0
        decision = r.crash_store.decide(fp_key, fp, cooldown_seconds, now)
        if not decision.should_send:
            return
        payload = _transport_mod.build_payload(
            project=r.cfg.project,
            fp=fp,
            exc=exc,
            tb=tb,
            first_seen=decision.first_seen,
            suppressed_count=decision.suppressed_count,
            with_locals=r.cfg.with_locals,
            regression=decision.was_resolved,
        )
        subject, body = _transport_mod.render(
            payload,
            cooldown_hours=r.cfg.cooldown_hours,
            markup=r.transport.markup,
        )
        ok = r.transport.send_with_deadline(subject, body, CRASH_TIMEOUT)
        if ok:
            r.crash_store.record_sent(fp_key, now)
    except Exception:
        # Crash path is best-effort; the process is dying. Log at debug
        # so configured users can see it, without interfering with the
        # original traceback the chained excepthook is about to print.
        log.debug("crash_handler failed", exc_info=True)


def _threading_handler(exc: BaseException, tb: TracebackType | None) -> None:
    try:
        if tb is not None and exc.__traceback__ is None:
            try:
                exc.__traceback__ = tb
            except Exception:
                pass
        report(exc)
    except Exception:
        pass


def _asyncio_handler(exc: BaseException) -> None:
    try:
        report(exc)
    except Exception:
        pass
