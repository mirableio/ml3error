"""Redis store tests. Skipped unless a Redis is reachable at localhost:6379."""
from __future__ import annotations

import uuid

import pytest

redis = pytest.importorskip("redis")


def _redis_up() -> bool:
    try:
        r = redis.from_url("redis://localhost:6379/0", socket_timeout=0.5)
        r.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_up(), reason="Redis not reachable")

FP = ("ValueError", "a.py", "main")


@pytest.fixture
def redis_store():
    import ml3error.store.redis as rmod
    from ml3error.store.redis import RedisStore

    # Isolate per-test under a unique prefix so list/scan stays clean.
    tag = uuid.uuid4().hex[:8]
    original_prefix = rmod._PREFIX
    rmod._PREFIX = f"ml3error_test_{tag}"

    store = RedisStore("redis://localhost:6379/0")
    yield store

    for key in store._r.scan_iter(f"{rmod._PREFIX}:*"):
        store._r.delete(key)
    store.close()
    rmod._PREFIX = original_prefix


def test_redis_first_then_suppressed(redis_store):
    now = 1000.0
    d = redis_store.decide("fp-A", FP, 60, now)
    assert d.should_send is True
    redis_store.record_sent("fp-A", now, d.suppressed_count)
    d2 = redis_store.decide("fp-A", FP, 60, now + 10)
    assert d2.should_send is False
    assert d2.suppressed_count == 1


def test_redis_record_sent_subtracts_not_clears(redis_store):
    now = 1000.0
    d0 = redis_store.decide("fp-B", FP, 60, now)
    redis_store.record_sent("fp-B", now, d0.suppressed_count)
    redis_store.decide("fp-B", FP, 60, now + 10)  # suppress -> count=1
    d = redis_store.decide("fp-B", FP, 60, now + 70)  # cooldown expired, report=1
    redis_store.bump_suppressed("fp-B")  # late bump -> count=2
    redis_store.record_sent("fp-B", now + 70, reported_count=d.suppressed_count)
    # Late bump survives.
    final = redis_store.decide("fp-B", FP, 60, now + 80)
    assert final.should_send is False
    assert final.suppressed_count == 2


def test_redis_subtract_counters_preserves_in_flight_bumps(redis_store):
    redis_store.bump_dropped()
    redis_store.bump_dropped()
    snapshot = redis_store.read_counters()
    assert snapshot[0] == 2
    redis_store.bump_dropped()  # the race: bump after snapshot
    redis_store.subtract_counters(*snapshot)
    assert redis_store.read_counters()[0] == 1


def test_redis_list_and_set_resolved(redis_store):
    redis_store.decide("fp-X", ("ValueError", "a.py", "fa"), 60, 1000.0)
    redis_store.decide("fp-Y", ("TypeError", "b.py", "fb"), 60, 1100.0)
    items = redis_store.list_fingerprints()
    fps = [it["fp"] for it in items]
    assert "fp-X" in fps and "fp-Y" in fps
    by_fp = {it["fp"]: it for it in items}
    assert by_fp["fp-X"]["exc_type"] == "ValueError"
    assert by_fp["fp-Y"]["rel_path"] == "b.py"

    redis_store.set_resolved("fp-X", True)
    unresolved = [it["fp"] for it in redis_store.list_fingerprints(resolved=False)]
    resolved = [it["fp"] for it in redis_store.list_fingerprints(resolved=True)]
    assert "fp-Y" in unresolved and "fp-X" not in unresolved
    assert "fp-X" in resolved


def test_redis_crash_store_record_sent_does_not_typeerror(redis_store):
    """Regression: RedisCrashStore.record_sent used to TypeError because
    it forwarded to RedisStore.record_sent which now requires reported_count."""
    from ml3error.store.redis import RedisCrashStore

    cs = RedisCrashStore("redis://localhost:6379/0")
    cs.decide("fp-crash", FP, 60, 1000.0)
    # Must not raise TypeError (or any other exception).
    cs.record_sent("fp-crash", 1000.0)
    cs.close()
