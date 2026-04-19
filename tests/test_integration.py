from __future__ import annotations

import logging
import time


def _wait_for(predicate, timeout=2.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def test_report_sends_once_then_suppresses(fresh_ml3error, tmp_project, sent_messages):
    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x", "chat_id": "y"},
        project_root=tmp_project,
        project="t1",
        heartbeat=False,
        hooks=[],
    )
    try:
        raise ValueError("boom")
    except ValueError as e:
        fresh_ml3error.report(e)

    assert _wait_for(lambda: len(sent_messages) >= 1)
    assert len(sent_messages) == 1

    # Same fingerprint within cooldown → suppressed
    try:
        raise ValueError("boom2")
    except ValueError as e:
        fresh_ml3error.report(e)
    time.sleep(0.1)
    assert len(sent_messages) == 1
    assert "t1" in sent_messages[0]["message"]


def test_inflight_bumps_suppressed_even_when_last_notified_is_null(
    fresh_ml3error, tmp_project, sent_messages
):
    """New fingerprint, mid-first-send: subsequent same-fp calls must count
    as suppressed, not silently ignored."""
    # Install a slow transport so the first send stays in-flight long enough
    # for us to report a second occurrence while it's still being "sent".
    release = __import__("threading").Event()

    def slow_send(subject, body):
        release.wait(timeout=2.0)
        return True

    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )
    fresh_ml3error._reporter.transport.send = slow_send  # type: ignore[attr-defined]

    try:
        raise ValueError("race")
    except ValueError as e:
        fresh_ml3error.report(e)

    # Worker is now blocked inside slow_send; fp is in_flight with last_notified still NULL.
    try:
        raise ValueError("race")
    except ValueError as e:
        fresh_ml3error.report(e)
    try:
        raise ValueError("race")
    except ValueError as e:
        fresh_ml3error.report(e)

    # Release the first send. When the worker calls record_sent, it subtracts
    # the reported_count (0) from suppressed_count (now 2), leaving 2.
    release.set()
    assert _wait_for(lambda: not fresh_ml3error._reporter.in_flight, timeout=2.0)

    # Verify the two suppressions survived.
    store = fresh_ml3error._reporter.store
    _, _, suppressed = store.read_counters()
    assert suppressed == 2


def test_different_fingerprint_sends_separately(fresh_ml3error, tmp_project, sent_messages):
    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x", "chat_id": "y"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )
    try:
        raise ValueError("v")
    except ValueError as e:
        fresh_ml3error.report(e)
    try:
        raise TypeError("t")
    except TypeError as e:
        fresh_ml3error.report(e)

    assert _wait_for(lambda: len(sent_messages) >= 2)
    assert len(sent_messages) == 2


def test_locals_override_suppresses_values(fresh_ml3error, tmp_project, sent_messages):
    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x", "chat_id": "y"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )
    secret = "s3cret_value"
    try:
        assert secret
        raise RuntimeError("nope")
    except RuntimeError as e:
        fresh_ml3error.report(e, locals=False)

    assert _wait_for(lambda: len(sent_messages) >= 1)
    assert "s3cret_value" not in sent_messages[0]["message"]


def test_ping_succeeds(fresh_ml3error, tmp_project, sent_messages):
    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )
    fresh_ml3error.ping()
    assert len(sent_messages) == 1
    assert "ping" in sent_messages[0]["message"].lower()


def test_ping_raises_on_transport_error(fresh_ml3error, tmp_project, fake_notifier):
    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )
    fake_notifier.errors_to_return = ["nope"]
    import pytest
    with pytest.raises(RuntimeError):
        fresh_ml3error.ping()


def test_report_after_close_raises(fresh_ml3error, tmp_project):
    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )
    fresh_ml3error.close()
    import pytest
    with pytest.raises(RuntimeError):
        fresh_ml3error.report(ValueError("x"))


def test_init_idempotent_same_args(fresh_ml3error, tmp_project):
    kwargs = dict(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )
    fresh_ml3error.init(**kwargs)
    fresh_ml3error.init(**kwargs)  # should not raise


def test_init_different_args_raises(fresh_ml3error, tmp_project):
    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        cooldown_hours=24,
        heartbeat=False,
        hooks=[],
    )
    import pytest
    with pytest.raises(RuntimeError):
        fresh_ml3error.init(
            transport="telegram",
            transport_config={"token": "x"},
            project_root=tmp_project,
            cooldown_hours=12,
            heartbeat=False,
            hooks=[],
        )


def test_store_failure_does_not_silence_alert(fresh_ml3error, tmp_project, sent_messages):
    """If store.decide raises, report() must still attempt a send rather
    than silently dropping the alert."""
    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )
    # Break the store.
    def boom(*a, **kw):
        raise RuntimeError("store down")
    fresh_ml3error._reporter.store.decide = boom  # type: ignore[attr-defined]

    try:
        raise ValueError("happens during outage")
    except ValueError as e:
        fresh_ml3error.report(e)

    assert _wait_for(lambda: len(sent_messages) >= 1)


def test_logging_handler_forwards_exceptions(fresh_ml3error, tmp_project, sent_messages):
    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )
    logger = logging.getLogger("ml3error.tests.handler")
    logger.handlers.clear()
    logger.addHandler(fresh_ml3error.LoggingHandler())
    logger.setLevel(logging.ERROR)

    try:
        raise RuntimeError("logged")
    except RuntimeError:
        logger.exception("bad thing")

    assert _wait_for(lambda: len(sent_messages) >= 1)
    assert "RuntimeError" in sent_messages[0]["message"]

    # Bare log.error WITHOUT exc_info still notifies — fingerprinted by
    # (level, pathname, funcName).
    before = len(sent_messages)
    logger.error("db connection dropped")
    assert _wait_for(lambda: len(sent_messages) > before)
    latest = sent_messages[-1]["message"]
    assert "ERROR" in latest
    assert "db connection dropped" in latest


def test_logging_handler_does_not_deadlock_on_ml3error_logs(
    fresh_ml3error, tmp_project, sent_messages
):
    """Regression: LoggingHandler at WARNING on the root logger used to
    deadlock when ml3error's own warnings were emitted while r.lock was
    held. The thread-local reentrance guard prevents this."""
    import logging as _logging
    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )
    # Install at WARNING on root so ml3error's internal warnings are
    # fed into the handler.
    root = _logging.getLogger()
    handler = fresh_ml3error.LoggingHandler(level=_logging.WARNING)
    root.addHandler(handler)
    try:
        # Break the store so decide() raises → ml3error logs a warning.
        fresh_ml3error._reporter.store.decide = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("store outage")
        )
        try:
            raise ValueError("through broken store")
        except ValueError as e:
            # Must return promptly — if the guard is missing this deadlocks.
            fresh_ml3error.report(e)
        # The alert itself still gets enqueued via the stub Decision fallback.
        assert _wait_for(lambda: len(sent_messages) >= 1)
    finally:
        root.removeHandler(handler)


def test_install_asyncio_hook_forwards_exceptions(
    fresh_ml3error, tmp_project, sent_messages
):
    """ml3error.install_asyncio_hook() should hook the running loop so
    asyncio's call_exception_handler() flows through report()."""
    import asyncio

    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],  # explicitly skip asyncio hook at init
    )

    async def body():
        fresh_ml3error.install_asyncio_hook()
        loop = asyncio.get_running_loop()
        try:
            raise RuntimeError("async task blew up")
        except RuntimeError as e:
            # Simulate asyncio noticing an unhandled task exception.
            loop.call_exception_handler({"message": "Task exception", "exception": e})
        # Give the worker a moment to send.
        await asyncio.sleep(0.3)

    asyncio.run(body())
    assert len(sent_messages) == 1
    assert "RuntimeError" in sent_messages[0]["message"]


def test_install_asyncio_hook_requires_init(fresh_ml3error, tmp_project):
    """Must raise RuntimeError before init() has been called."""
    import pytest
    with pytest.raises(RuntimeError):
        fresh_ml3error.install_asyncio_hook()


def test_worker_warning_does_not_deadlock_with_logging_handler(
    fresh_ml3error, tmp_project, sent_messages
):
    """Regression: the worker's `log.warning(..., exc_info=True)` after
    a failed post-send writeback used to deadlock the worker when
    LoggingHandler was attached at WARNING, because the handler would
    call back into report() on the worker thread and try to re-acquire
    self._lock. The _LockWithGuard context manager prevents this."""
    import logging as _logging

    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )

    root = _logging.getLogger()
    handler = fresh_ml3error.LoggingHandler(level=_logging.WARNING)
    root.addHandler(handler)
    try:
        # Break record_sent so the worker hits its except + log.warning.
        def boom(*a, **kw):
            raise RuntimeError("writeback outage")
        fresh_ml3error._reporter.store.record_sent = boom

        try:
            raise ValueError("worker-deadlock regression")
        except ValueError as e:
            fresh_ml3error.report(e)

        # Worker must process the item (send succeeds, writeback fails)
        # and then discard from in_flight — not wedge forever.
        assert _wait_for(
            lambda: not fresh_ml3error._reporter.in_flight,
            timeout=3.0,
        ), "worker thread is still wedged holding the lock"
        assert len(sent_messages) == 1
    finally:
        root.removeHandler(handler)


def test_log_record_fingerprint_distinguishes_call_sites(
    fresh_ml3error, tmp_project, sent_messages
):
    """Two bare log.error calls in the same function must dedup as
    separate fingerprints because of their line numbers."""
    import logging as _logging
    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )
    logger = _logging.getLogger("ml3error.tests.sites")
    logger.handlers.clear()
    logger.addHandler(fresh_ml3error.LoggingHandler())
    logger.setLevel(_logging.ERROR)

    logger.error("site A")
    logger.error("site B")

    assert _wait_for(lambda: len(sent_messages) >= 2)
    bodies = [m["message"] for m in sent_messages]
    assert any("site A" in b for b in bodies)
    assert any("site B" in b for b in bodies)


def test_logging_handler_dedupes_bare_log_errors(fresh_ml3error, tmp_project, sent_messages):
    """Same call site + same level should fingerprint the same: first one
    notifies, subsequent ones within cooldown are suppressed."""
    fresh_ml3error.init(
        transport="telegram",
        transport_config={"token": "x"},
        project_root=tmp_project,
        heartbeat=False,
        hooks=[],
    )
    logger = logging.getLogger("ml3error.tests.dedup")
    logger.handlers.clear()
    logger.addHandler(fresh_ml3error.LoggingHandler())
    logger.setLevel(logging.ERROR)

    def log_it(i):
        logger.error("db offline (attempt %d)", i)

    log_it(1)
    log_it(2)
    log_it(3)
    assert _wait_for(lambda: len(sent_messages) >= 1)
    # Give any stragglers time to land.
    time.sleep(0.15)
    assert len(sent_messages) == 1
