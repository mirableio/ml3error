from __future__ import annotations

import os
import sqlite3
import time

from .. import constants
from .base import Decision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fingerprints (
    fp TEXT PRIMARY KEY,
    exc_type TEXT NOT NULL DEFAULT '',
    rel_path TEXT NOT NULL DEFAULT '',
    func_name TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_notified REAL,
    last_activity REAL NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,
    suppressed_count INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    last_message TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_COUNTERS = ("dropped", "transport_failures", "suppressed")


def _open(path: str, *, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open a connection tuned for this library's access pattern.

    - check_same_thread=False: connection is shared across the worker,
      heartbeat, and caller threads. The caller is responsible for
      external serialization (SQLiteStore assumes a module-level lock;
      SQLiteCrashStore relies on busy_timeout at the file level instead).
    - isolation_level=None: autocommit mode. Every statement is its own
      tx, which suits our short read/write operations and keeps WAL
      contention minimal.
    - WAL + busy_timeout handle concurrent-reader-with-writer cases.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    conn.executescript(_SCHEMA)
    return conn


def _meta_int(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None or row[0] is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _meta_float(conn: sqlite3.Connection, key: str) -> float | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _meta_bump(conn: sqlite3.Connection, key: str, delta: int = 1) -> None:
    current = _meta_int(conn, key)
    _meta_set(conn, key, str(current + delta))


class SQLiteStore:
    def __init__(self, path: str):
        self._path = path
        self._conn = _open(path, busy_timeout_ms=5000)
        self.prune(time.time(), constants.STATE_PRUNE_DAYS * 86400)

    def decide(
        self,
        fp_key: str,
        fp: tuple[str, str, str],
        cooldown_seconds: float,
        now: float,
    ) -> Decision:
        exc_type, rel_path, func_name = fp
        row = self._conn.execute(
            "SELECT first_seen, last_notified, suppressed_count, resolved "
            "FROM fingerprints WHERE fp=?",
            (fp_key,),
        ).fetchone()

        if row is None:
            self._conn.execute(
                "INSERT INTO fingerprints"
                "(fp, exc_type, rel_path, func_name, first_seen, last_activity, "
                "total_count, suppressed_count) "
                "VALUES(?, ?, ?, ?, ?, ?, 1, 0)",
                (fp_key, exc_type, rel_path, func_name, now, now),
            )
            return Decision(should_send=True, first_seen=now, suppressed_count=0)

        first_seen, last_notified, suppressed_count, resolved = row
        was_resolved = bool(resolved)
        cooldown_expired = (
            last_notified is None or (now - last_notified) >= cooldown_seconds
        )
        # Bypass cooldown when a resolved fp regresses — the user
        # explicitly said "fixed", so the very next occurrence deserves
        # an immediate alert with the REOPENED banner. Without this,
        # in-cooldown regressions silently clear `resolved` and the
        # banner never reaches the user when cooldown later expires.
        if cooldown_expired or was_resolved:
            self._conn.execute(
                "UPDATE fingerprints SET last_activity=?, "
                "total_count = total_count + 1, resolved=0 WHERE fp=?",
                (now, fp_key),
            )
            return Decision(
                should_send=True,
                first_seen=first_seen,
                suppressed_count=int(suppressed_count),
                was_resolved=was_resolved,
            )

        self._conn.execute(
            "UPDATE fingerprints SET suppressed_count = suppressed_count + 1, "
            "total_count = total_count + 1, last_activity=?, resolved=0 WHERE fp=?",
            (now, fp_key),
        )
        _meta_bump(self._conn, "suppressed")
        return Decision(
            should_send=False,
            first_seen=first_seen,
            suppressed_count=int(suppressed_count) + 1,
            was_resolved=False,  # not resolved at decision time anyway
        )

    def record_sent(self, fp_key: str, now: float, reported_count: int) -> None:
        # Subtract (don't clear) so that suppressions bumped between payload
        # render and actual send survive and roll into the next report.
        self._conn.execute(
            "UPDATE fingerprints SET last_notified=?, last_activity=?, "
            "suppressed_count = MAX(suppressed_count - ?, 0) WHERE fp=?",
            (now, now, reported_count, fp_key),
        )

    def bump_suppressed(self, fp_key: str) -> None:
        now = time.time()
        # Same auto-unresolve rule as decide() — a real occurrence
        # flips a resolved fingerprint back to unresolved.
        self._conn.execute(
            "UPDATE fingerprints SET resolved=0 WHERE fp=?",
            (fp_key,),
        )
        self._conn.execute(
            "UPDATE fingerprints SET suppressed_count = suppressed_count + 1, "
            "total_count = total_count + 1, last_activity=? WHERE fp=?",
            (now, fp_key),
        )
        _meta_bump(self._conn, "suppressed")

    def bump_transport_failures(self) -> None:
        _meta_bump(self._conn, "transport_failures")

    def bump_dropped(self) -> None:
        _meta_bump(self._conn, "dropped")

    def read_counters(self) -> tuple[int, int, int]:
        return (
            _meta_int(self._conn, "dropped"),
            _meta_int(self._conn, "transport_failures"),
            _meta_int(self._conn, "suppressed"),
        )

    def subtract_counters(self, dropped: int, fails: int, suppressed: int) -> None:
        for key, amount in zip(_COUNTERS, (dropped, fails, suppressed)):
            if amount <= 0:
                continue
            current = _meta_int(self._conn, key)
            _meta_set(self._conn, key, str(max(current - amount, 0)))

    def get_last_heartbeat(self) -> float | None:
        return _meta_float(self._conn, "last_heartbeat")

    def set_last_heartbeat(self, ts: float) -> None:
        _meta_set(self._conn, "last_heartbeat", str(ts))

    def claim_heartbeat(self, ts: float) -> tuple[bool, float | None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            last = _meta_float(self._conn, "last_heartbeat")
            if last is not None and last >= ts:
                self._conn.execute("COMMIT")
                return False, last
            _meta_set(self._conn, "last_heartbeat", str(ts))
            self._conn.execute("COMMIT")
            return True, last
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def prune(self, now: float, max_age_seconds: float) -> None:
        # Prune on last_activity, which is touched on every occurrence
        # (send-attempted or suppressed). A fingerprint that keeps firing
        # is kept even if delivery has been failing for weeks.
        cutoff = now - max_age_seconds
        self._conn.execute(
            "DELETE FROM fingerprints WHERE last_activity < ?",
            (cutoff,),
        )

    # --- Review UI helpers ---

    def list_fingerprints(self, *, resolved: bool | None = None) -> list[dict]:
        """Return all fingerprints, optionally filtered by resolved flag.

        resolved=None → all; True/False → filter. Most-recently-active first.
        """
        sql = (
            "SELECT fp, exc_type, rel_path, func_name, first_seen, last_notified, "
            "last_activity, total_count, suppressed_count, resolved, last_message "
            "FROM fingerprints"
        )
        params: tuple = ()
        if resolved is not None:
            sql += " WHERE resolved=?"
            params = (1 if resolved else 0,)
        sql += " ORDER BY last_activity DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "fp": r[0],
                "exc_type": r[1],
                "rel_path": r[2],
                "func_name": r[3],
                "first_seen": r[4],
                "last_notified": r[5],
                "last_activity": r[6],
                "total_count": int(r[7]),
                "suppressed_count": int(r[8]),
                "resolved": bool(r[9]),
                "last_message": r[10] or "",
            }
            for r in rows
        ]

    def set_resolved(self, fp_key: str, resolved: bool) -> None:
        self._conn.execute(
            "UPDATE fingerprints SET resolved=? WHERE fp=?",
            (1 if resolved else 0, fp_key),
        )

    def set_last_message(self, fp_key: str, message: str) -> None:
        self._conn.execute(
            "UPDATE fingerprints SET last_message=? WHERE fp=?",
            (message, fp_key),
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    @property
    def path(self) -> str:
        return self._path


class SQLiteCrashStore:
    """Lockless SQLite store for the excepthook path. Best-effort on contention."""

    def __init__(self, path: str):
        self._path = path
        self._conn: sqlite3.Connection | None = None

    def _ensure(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _open(self._path, busy_timeout_ms=constants.SQLITE_BUSY_TIMEOUT_MS)
        return self._conn

    def decide(
        self,
        fp_key: str,
        fp: tuple[str, str, str],
        cooldown_seconds: float,
        now: float,
    ) -> Decision:
        exc_type, rel_path, func_name = fp
        try:
            conn = self._ensure()
            row = conn.execute(
                "SELECT first_seen, last_notified, suppressed_count, resolved "
                "FROM fingerprints WHERE fp=?",
                (fp_key,),
            ).fetchone()
        except sqlite3.Error:
            return Decision(should_send=True, first_seen=now, suppressed_count=0)

        if row is None:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO fingerprints"
                    "(fp, exc_type, rel_path, func_name, first_seen, last_activity, "
                    "total_count, suppressed_count) "
                    "VALUES(?, ?, ?, ?, ?, ?, 1, 0)",
                    (fp_key, exc_type, rel_path, func_name, now, now),
                )
            except sqlite3.Error:
                pass
            return Decision(should_send=True, first_seen=now, suppressed_count=0)

        first_seen, last_notified, suppressed_count, resolved = row
        was_resolved = bool(resolved)
        cooldown_expired = (
            last_notified is None or (now - last_notified) >= cooldown_seconds
        )
        # Bypass cooldown for resolved regressions — see SQLiteStore.decide.
        if cooldown_expired or was_resolved:
            try:
                conn.execute(
                    "UPDATE fingerprints SET last_activity=?, "
                    "total_count = total_count + 1, resolved=0 WHERE fp=?",
                    (now, fp_key),
                )
            except sqlite3.Error:
                pass
            return Decision(
                should_send=True,
                first_seen=first_seen,
                suppressed_count=int(suppressed_count),
                was_resolved=was_resolved,
            )
        # Inside cooldown: same semantics as SQLiteStore.decide.
        try:
            conn.execute(
                "UPDATE fingerprints SET suppressed_count = suppressed_count + 1, "
                "total_count = total_count + 1, last_activity=?, resolved=0 WHERE fp=?",
                (now, fp_key),
            )
            _meta_bump(conn, "suppressed")
        except sqlite3.Error:
            pass
        return Decision(
            should_send=False,
            first_seen=first_seen,
            suppressed_count=int(suppressed_count) + 1,
            was_resolved=False,
        )

    def record_sent(self, fp_key: str, now: float) -> None:
        # Crash path only runs once per process death, so a plain clear
        # of suppressed_count is fine — no concurrent writer to race with.
        # last_activity is also advanced so pruning stays accurate.
        try:
            conn = self._ensure()
            conn.execute(
                "UPDATE fingerprints SET last_notified=?, last_activity=?, "
                "suppressed_count=0 WHERE fp=?",
                (now, now, fp_key),
            )
        except sqlite3.Error:
            pass

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
