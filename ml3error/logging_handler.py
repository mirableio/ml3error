from __future__ import annotations

import logging


class LoggingHandler(logging.Handler):
    """Forwards log records to ml3error.

    Records carrying `exc_info` go through `ml3error.report(exc)` — same
    fingerprint and payload as if the user called report() directly.

    Records without `exc_info` (plain `log.error("db down")`) are also
    reported, fingerprinted by (level, pathname, funcName) so different
    call sites dedup independently. The outgoing message uses the log
    level as its 'type' and the record's formatted message as its body.
    """

    def __init__(self, level: int = logging.ERROR):
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        # Reentrance guard: when a log record arrives on a thread that
        # is already inside any reporter critical section (holding
        # r.lock), forwarding it back through report() would try to
        # re-acquire the same lock and deadlock. The thread-local flag
        # is set by both the caller path (_enqueue) and the worker's
        # post-send writeback, via _LockWithGuard.
        from . import _is_in_critical

        if _is_in_critical():
            return
        try:
            if record.exc_info and record.exc_info[1] is not None:
                from . import report  # local import to avoid circular

                report(record.exc_info[1])
                return
            from . import _report_log_record

            _report_log_record(record)
        except Exception:
            self.handleError(record)
