import time

from ml3error.store.sqlite import SQLiteCrashStore, SQLiteStore

FP = ("ValueError", "app.py", "main")


def test_first_occurrence_sends(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    d = store.decide("fp1", FP, cooldown_seconds=60, now=time.time())
    assert d.should_send is True
    assert d.suppressed_count == 0


def test_second_occurrence_within_cooldown_suppresses(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    now = 1000.0
    d = store.decide("fp1", FP, 60, now)
    assert d.should_send is True
    store.record_sent("fp1", now, d.suppressed_count)
    d2 = store.decide("fp1", FP, 60, now + 10)
    assert d2.should_send is False
    assert d2.suppressed_count == 1
    d3 = store.decide("fp1", FP, 60, now + 20)
    assert d3.should_send is False
    assert d3.suppressed_count == 2


def test_cooldown_expiry_allows_send(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    now = 1000.0
    d0 = store.decide("fp1", FP, 60, now)
    store.record_sent("fp1", now, d0.suppressed_count)
    d = store.decide("fp1", FP, 60, now + 61)
    assert d.should_send is True


def test_record_sent_subtracts_reported_count_preserving_late_bumps(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    now = 1000.0
    d0 = store.decide("fp1", FP, 60, now)  # first fire
    store.record_sent("fp1", now, d0.suppressed_count)
    store.decide("fp1", FP, 60, now + 10)  # suppress (count=1)
    d = store.decide("fp1", FP, 60, now + 70)  # cooldown expired; report count=1
    # Simulate a late bump that happens between render and actual send.
    store.bump_suppressed("fp1")  # count=2 now
    store.record_sent("fp1", now + 70, reported_count=d.suppressed_count)
    # Late bump should survive.
    d_final = store.decide("fp1", FP, 60, now + 80)
    assert d_final.should_send is False
    assert d_final.suppressed_count == 2  # 1 late-bump + 1 new suppression


def test_bump_suppressed(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    store.decide("fp1", FP, 60, 1000.0)
    store.bump_suppressed("fp1")
    store.bump_suppressed("fp1")
    _, _, suppressed = store.read_counters()
    assert suppressed == 2


def test_counters(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    store.bump_dropped()
    store.bump_dropped()
    store.bump_transport_failures()
    dropped, fails, suppressed = store.read_counters()
    assert (dropped, fails, suppressed) == (2, 1, 0)
    store.subtract_counters(dropped, fails, suppressed)
    assert store.read_counters() == (0, 0, 0)


def test_subtract_counters_preserves_in_flight_bumps(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    store.bump_dropped()
    store.bump_dropped()
    snapshot = store.read_counters()
    store.bump_dropped()
    store.subtract_counters(*snapshot)
    assert store.read_counters() == (1, 0, 0)


def test_heartbeat_roundtrip(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    assert store.get_last_heartbeat() is None
    store.set_last_heartbeat(123.5)
    assert store.get_last_heartbeat() == 123.5


def test_prune_removes_stale_only(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    now = 1_000_000.0
    old = now - 40 * 86400
    recent = now - 10 * 86400
    store.decide("old", FP, 60, old)
    store.decide("recent", FP, 60, recent)
    store.prune(now, 30 * 86400)
    d1 = store.decide("old", FP, 60, now)
    d2 = store.decide("recent", FP, 60, now)
    assert d1.first_seen == now
    assert d2.first_seen == recent


def test_prune_keeps_active_fp_with_failing_sends(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    t0 = 1_000_000.0
    store.decide("active", FP, 60, t0)
    store.bump_suppressed("active")
    now = t0 + 40 * 86400
    store._conn.execute(
        "UPDATE fingerprints SET last_activity=? WHERE fp=?",
        (now - 3600, "active"),
    )
    store.prune(now, 30 * 86400)
    d = store.decide("active", FP, 60, now)
    assert d.first_seen == t0


def test_total_count_tracks_every_occurrence(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    now = 1000.0
    # First occurrence -> total=1
    d0 = store.decide("fp1", FP, 60, now)
    store.record_sent("fp1", now, d0.suppressed_count)
    # Two suppressed in cooldown -> total=3
    store.decide("fp1", FP, 60, now + 10)
    store.decide("fp1", FP, 60, now + 20)
    # One in-flight bump -> total=4
    store.bump_suppressed("fp1")
    # Cooldown expires, another send -> total=5
    store.record_sent("fp1", now + 100, 0)
    d = store.decide("fp1", FP, 60, now + 200)
    assert d.should_send is True

    items = store.list_fingerprints()
    assert items[0]["total_count"] == 5


def test_crash_store_cooldown_bumps_suppressed(tmp_path):
    """Crash-path inside-cooldown occurrences must count against
    suppressed/total — same contract as SQLiteStore.decide."""
    store = SQLiteStore(str(tmp_path / "s.db"))
    crash = SQLiteCrashStore(str(tmp_path / "s.db"))
    now = 1000.0
    d0 = store.decide("fp-cx", FP, 60, now)
    store.record_sent("fp-cx", now, d0.suppressed_count)
    # Crash re-fires inside cooldown.
    d1 = crash.decide("fp-cx", FP, 60, now + 10)
    assert d1.should_send is False
    d2 = crash.decide("fp-cx", FP, 60, now + 20)
    assert d2.should_send is False

    items = store.list_fingerprints()
    row = next(it for it in items if it["fp"] == "fp-cx")
    assert row["suppressed_count"] == 2
    assert row["total_count"] == 3  # initial + 2 crash-path suppressions


def test_crash_store_record_sent_touches_last_activity(tmp_path):
    """Crash-path record_sent must advance last_activity so crash-only
    fingerprints don't get pruned as inactive."""
    # SQLiteStore.__init__ runs prune() relative to current wall time,
    # so use near-now timestamps rather than small literals.
    now = time.time()
    crash = SQLiteCrashStore(str(tmp_path / "s.db"))
    crash.decide("fp-cy", FP, 60, now - 3600)
    crash.record_sent("fp-cy", now)

    store = SQLiteStore(str(tmp_path / "s.db"))
    row = next(it for it in store.list_fingerprints() if it["fp"] == "fp-cy")
    assert row["last_activity"] == now


def test_resolved_auto_clears_on_next_occurrence(tmp_path):
    """Resolved is sticky from the user's perspective only until the
    fingerprint fires again — then it auto-unresolves and the Decision
    surfaces was_resolved so the message can show a regression banner."""
    store = SQLiteStore(str(tmp_path / "s.db"))
    now = 1_000_000.0
    d0 = store.decide("fp1", FP, 60, now)
    store.record_sent("fp1", now, d0.suppressed_count)
    store.set_resolved("fp1", True)

    # Fire again outside cooldown — should unresolve and report regression.
    d1 = store.decide("fp1", FP, 60, now + 1000)
    assert d1.should_send is True
    assert d1.was_resolved is True

    items = store.list_fingerprints()
    fp_row = next(it for it in items if it["fp"] == "fp1")
    assert fp_row["resolved"] is False  # auto-cleared


def test_resolved_regression_bypasses_cooldown(tmp_path):
    """A resolved fp firing inside cooldown must yield should_send=True
    (with was_resolved=True) so the user sees an immediate REOPENED
    alert. Without this the in-cooldown fire silently clears resolved
    and the next post-cooldown send wouldn't carry the regression flag."""
    store = SQLiteStore(str(tmp_path / "s.db"))
    now = 1_000_000.0
    d0 = store.decide("fp1", FP, 60, now)
    store.record_sent("fp1", now, d0.suppressed_count)
    store.set_resolved("fp1", True)

    # Firing again at now+10 → still inside the 60s cooldown.
    d = store.decide("fp1", FP, 60, now + 10)
    assert d.should_send is True
    assert d.was_resolved is True

    # resolved is cleared after the regression decision.
    items = store.list_fingerprints()
    assert next(it for it in items if it["fp"] == "fp1")["resolved"] is False


def test_list_and_set_resolved(tmp_path):
    store = SQLiteStore(str(tmp_path / "s.db"))
    store.decide("fpA", ("ValueError", "a.py", "fa"), 60, 1000.0)
    store.decide("fpB", ("TypeError", "b.py", "fb"), 60, 1100.0)

    items = store.list_fingerprints()
    assert [it["fp"] for it in items] == ["fpB", "fpA"]  # most recent first
    assert items[0]["exc_type"] == "TypeError"
    assert items[0]["rel_path"] == "b.py"
    assert items[0]["resolved"] is False

    store.set_resolved("fpA", True)
    assert [it["fp"] for it in store.list_fingerprints(resolved=False)] == ["fpB"]
    assert [it["fp"] for it in store.list_fingerprints(resolved=True)] == ["fpA"]
