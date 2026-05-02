from __future__ import annotations

import time

from .. import constants
from .base import Decision

try:
    import redis as _redis
except ImportError as e:
    raise ImportError(
        "Redis state backend requires the 'redis' extra: "
        "uv add 'ml3error[redis]' or pip install 'ml3error[redis]'"
    ) from e


_TTL = constants.STATE_PRUNE_DAYS * 86400
_COUNTERS = ("dropped", "transport_failures", "suppressed")

# All keys live under this prefix. Tests monkey-patch this to isolate state.
_PREFIX = "ml3error"


def _fp_key(fp: str) -> str:
    return f"{_PREFIX}:fp:{fp}"


def _fp_pattern() -> str:
    return f"{_PREFIX}:fp:*"


def _meta_key() -> str:
    return f"{_PREFIX}:meta"


class RedisStore:
    def __init__(self, url: str):
        self._r = _redis.from_url(url, decode_responses=True)
        self._r.ping()

    def decide(
        self,
        fp_key: str,
        fp: tuple[str, str, str],
        cooldown_seconds: float,
        now: float,
    ):
        exc_type, rel_path, func_name = fp
        key = _fp_key(fp_key)
        data = self._r.hgetall(key)

        if not data:
            self._r.hset(
                key,
                mapping={
                    "exc_type": exc_type,
                    "rel_path": rel_path,
                    "func_name": func_name,
                    "first_seen": now,
                    "last_activity": now,
                    "total_count": 1,
                    "suppressed_count": 0,
                    "resolved": 0,
                },
            )
            self._r.expire(key, _TTL)
            return Decision(should_send=True, first_seen=now, suppressed_count=0)

        first_seen = float(data.get("first_seen", now))
        last_notified_s = data.get("last_notified")
        last_notified = float(last_notified_s) if last_notified_s else None
        suppressed_count = int(data.get("suppressed_count", 0))
        was_resolved = bool(int(data.get("resolved", 0)))
        cooldown_expired = (
            last_notified is None or (now - last_notified) >= cooldown_seconds
        )

        # Bypass cooldown for resolved regressions — see SQLiteStore.decide.
        if cooldown_expired or was_resolved:
            with self._r.pipeline() as pipe:
                pipe.hset(key, "last_activity", now)
                pipe.hset(key, "resolved", 0)
                pipe.hincrby(key, "total_count", 1)
                pipe.expire(key, _TTL)
                pipe.execute()
            return Decision(
                should_send=True,
                first_seen=first_seen,
                suppressed_count=suppressed_count,
                was_resolved=was_resolved,
            )

        with self._r.pipeline() as pipe:
            pipe.hset(key, "last_activity", now)
            pipe.hset(key, "resolved", 0)
            pipe.hincrby(key, "suppressed_count", 1)
            pipe.hincrby(key, "total_count", 1)
            pipe.hincrby(_meta_key(), "suppressed", 1)
            pipe.expire(key, _TTL)
            _, _, new_count, _, _, _ = pipe.execute()
        return Decision(
            should_send=False,
            first_seen=first_seen,
            suppressed_count=int(new_count),
            was_resolved=False,
        )

    def record_sent(self, fp_key: str, now: float, reported_count: int) -> None:
        # Subtract (don't clear) to preserve bumps that happened between
        # payload render and actual send. Advance last_activity too so
        # the review UI and prune both see the fingerprint as fresh.
        key = _fp_key(fp_key)
        with self._r.pipeline() as pipe:
            pipe.hset(key, mapping={"last_notified": now, "last_activity": now})
            pipe.hincrby(key, "suppressed_count", -reported_count)
            pipe.expire(key, _TTL)
            pipe.execute()
        # Clamp to zero in case of any underflow.
        current = int(self._r.hget(key, "suppressed_count") or 0)
        if current < 0:
            self._r.hset(key, "suppressed_count", 0)

    def bump_suppressed(self, fp_key: str) -> None:
        key = _fp_key(fp_key)
        with self._r.pipeline() as pipe:
            pipe.hset(key, mapping={"last_activity": time.time(), "resolved": 0})
            pipe.hincrby(key, "suppressed_count", 1)
            pipe.hincrby(key, "total_count", 1)
            pipe.hincrby(_meta_key(), "suppressed", 1)
            pipe.expire(key, _TTL)
            pipe.execute()

    def bump_transport_failures(self) -> None:
        self._r.hincrby(_meta_key(), "transport_failures", 1)

    def bump_dropped(self) -> None:
        self._r.hincrby(_meta_key(), "dropped", 1)

    def read_counters(self) -> tuple[int, int, int]:
        data = self._r.hgetall(_meta_key())
        return (
            int(data.get("dropped", 0)),
            int(data.get("transport_failures", 0)),
            int(data.get("suppressed", 0)),
        )

    def subtract_counters(self, dropped: int, fails: int, suppressed: int) -> None:
        for key, amount in zip(_COUNTERS, (dropped, fails, suppressed)):
            if amount <= 0:
                continue
            new_val = self._r.hincrby(_meta_key(), key, -amount)
            if new_val < 0:
                self._r.hset(_meta_key(), key, 0)

    def get_last_heartbeat(self) -> float | None:
        v = self._r.hget(_meta_key(), "last_heartbeat")
        return float(v) if v else None

    def set_last_heartbeat(self, ts: float) -> None:
        self._r.hset(_meta_key(), "last_heartbeat", ts)

    def prune(self, now: float, max_age_seconds: float) -> None:
        # TTL handles this on Redis.
        pass

    # --- Review UI helpers ---

    def list_fingerprints(self, *, resolved: bool | None = None) -> list[dict]:
        out: list[dict] = []
        for key in self._r.scan_iter(_fp_pattern()):
            data = self._r.hgetall(key)
            if not data:
                continue
            entry_resolved = bool(int(data.get("resolved", 0)))
            if resolved is not None and entry_resolved != resolved:
                continue
            last_notified_s = data.get("last_notified")
            out.append(
                {
                    "fp": key.split(":fp:", 1)[-1],
                    "exc_type": data.get("exc_type", ""),
                    "rel_path": data.get("rel_path", ""),
                    "func_name": data.get("func_name", ""),
                    "first_seen": float(data.get("first_seen", 0) or 0),
                    "last_notified": float(last_notified_s) if last_notified_s else None,
                    "last_activity": float(
                        data.get("last_activity", last_notified_s or data.get("first_seen", 0)) or 0
                    ),
                    "total_count": int(data.get("total_count", 0)),
                    "suppressed_count": int(data.get("suppressed_count", 0)),
                    "resolved": entry_resolved,
                    "last_message": data.get("last_message", ""),
                }
            )
        out.sort(key=lambda e: e["last_activity"], reverse=True)
        return out

    def set_resolved(self, fp_key: str, resolved: bool) -> None:
        self._r.hset(_fp_key(fp_key), "resolved", 1 if resolved else 0)
        self._r.expire(_fp_key(fp_key), _TTL)

    def set_last_message(self, fp_key: str, message: str) -> None:
        self._r.hset(_fp_key(fp_key), "last_message", message)
        self._r.expire(_fp_key(fp_key), _TTL)

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:
            pass


class RedisCrashStore:
    """Crash-path store for Redis. Uses its own connection with a short
    socket timeout so a slow Redis can't burn the dying process's budget,
    and clears suppressed_count on record_sent (no racing writer to preserve).
    """

    def __init__(self, url: str):
        self._r = _redis.from_url(url, decode_responses=True, socket_timeout=1.0)

    def decide(
        self,
        fp_key: str,
        fp: tuple[str, str, str],
        cooldown_seconds: float,
        now: float,
    ):
        exc_type, rel_path, func_name = fp
        try:
            key = _fp_key(fp_key)
            data = self._r.hgetall(key)
        except Exception:
            return Decision(should_send=True, first_seen=now, suppressed_count=0)

        if not data:
            try:
                self._r.hset(
                    key,
                    mapping={
                        "exc_type": exc_type,
                        "rel_path": rel_path,
                        "func_name": func_name,
                        "first_seen": now,
                        "last_activity": now,
                        "total_count": 1,
                        "suppressed_count": 0,
                        "resolved": 0,
                    },
                )
                self._r.expire(key, _TTL)
            except Exception:
                pass
            return Decision(should_send=True, first_seen=now, suppressed_count=0)

        first_seen = float(data.get("first_seen", now))
        last_notified_s = data.get("last_notified")
        last_notified = float(last_notified_s) if last_notified_s else None
        suppressed_count = int(data.get("suppressed_count", 0))
        was_resolved = bool(int(data.get("resolved", 0)))
        cooldown_expired = (
            last_notified is None or (now - last_notified) >= cooldown_seconds
        )
        # Bypass cooldown for resolved regressions — see RedisStore.decide.
        if cooldown_expired or was_resolved:
            try:
                self._r.hset(key, mapping={"last_activity": now, "resolved": 0})
                self._r.hincrby(key, "total_count", 1)
                self._r.expire(key, _TTL)
            except Exception:
                pass
            return Decision(
                should_send=True,
                first_seen=first_seen,
                suppressed_count=suppressed_count,
                was_resolved=was_resolved,
            )
        # Inside cooldown — same semantics as RedisStore.decide.
        try:
            self._r.hset(key, mapping={"last_activity": now, "resolved": 0})
            new_count = self._r.hincrby(key, "suppressed_count", 1)
            self._r.hincrby(key, "total_count", 1)
            self._r.hincrby(_meta_key(), "suppressed", 1)
            self._r.expire(key, _TTL)
            suppressed_count = int(new_count)
        except Exception:
            pass
        return Decision(
            should_send=False,
            first_seen=first_seen,
            suppressed_count=suppressed_count,
            was_resolved=False,
        )

    def record_sent(self, fp_key: str, now: float) -> None:
        try:
            key = _fp_key(fp_key)
            self._r.hset(
                key,
                mapping={"last_notified": now, "last_activity": now, "suppressed_count": 0},
            )
            self._r.expire(key, _TTL)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:
            pass
