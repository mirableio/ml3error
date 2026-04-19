from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Decision:
    should_send: bool
    first_seen: float
    suppressed_count: int


class Store(Protocol):
    """State backend. Methods are NOT thread-safe; the caller must hold a lock."""

    def decide(
        self,
        fp_key: str,
        fp: tuple[str, str, str],
        cooldown_seconds: float,
        now: float,
    ) -> Decision:
        """Read fingerprint state, decide whether to send, record occurrence.

        `fp` is the (exception_type, rel_path, func_name) tuple; the store
        persists these display fields on first insert so the review UI
        can render human-readable rows.

        If the fingerprint is inside its cooldown window, the fingerprint's
        `suppressed_count` and the global `suppressed` counter are both
        incremented, and `should_send=False` is returned.

        If the fingerprint is new or its cooldown has expired,
        `should_send=True` is returned. `first_seen` is created for new
        fingerprints. `last_notified` is NOT advanced here — the caller
        does that via `record_sent()` only on confirmed successful send.
        """
        ...

    def record_sent(self, fp_key: str, now: float, reported_count: int) -> None:
        """Mark a successful send: set last_notified=now, subtract
        `reported_count` from suppressed_count.

        Subtracting (rather than clearing) preserves any bumps that
        happened between the caller rendering the payload and the worker
        finishing the send — those are new suppressions that should roll
        into the next message.
        """
        ...

    def bump_suppressed(self, fp_key: str) -> None:
        """Bump fingerprints.suppressed_count and meta.suppressed together.

        Used when an occurrence is suppressed because the same fingerprint
        is already in-flight in the worker queue.
        """
        ...

    def bump_transport_failures(self) -> None: ...
    def bump_dropped(self) -> None: ...

    def read_counters(self) -> tuple[int, int, int]:
        """Returns (dropped, transport_failures, suppressed)."""
        ...

    def subtract_counters(self, dropped: int, fails: int, suppressed: int) -> None:
        """Subtract the given amounts from the three running counters,
        clamping each at zero. Used after a successful heartbeat send to
        remove exactly the values that were just reported, preserving any
        bumps that happened between read_counters() and here.
        """
        ...

    def get_last_heartbeat(self) -> float | None: ...
    def set_last_heartbeat(self, ts: float) -> None: ...

    def prune(self, now: float, max_age_seconds: float) -> None: ...

    # Review-UI methods (used by the `ml3error web` server).
    def list_fingerprints(self, *, resolved: bool | None = None) -> list[dict]: ...
    def set_resolved(self, fp_key: str, resolved: bool) -> None: ...
    def set_last_message(self, fp_key: str, message: str) -> None:
        """Store the most recently rendered message body for a fingerprint.

        Overwrites on every render so the review UI can show the latest
        details (traceback, locals, etc.) — the same content that was
        sent to the configured transport.
        """
        ...

    def close(self) -> None: ...


class CrashStore(Protocol):
    """Minimal lockless store used in the sys.excepthook path only."""

    def decide(
        self,
        fp_key: str,
        fp: tuple[str, str, str],
        cooldown_seconds: float,
        now: float,
    ) -> Decision:
        """Same semantics as Store.decide, but best-effort on contention."""
        ...

    def record_sent(self, fp_key: str, now: float) -> None:
        """Same as Store.record_sent but does not return suppressed_count."""
        ...

    def close(self) -> None: ...
