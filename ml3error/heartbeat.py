from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, time as dt_time

from . import constants
from .store.base import Store
from .transport import Transport

log = logging.getLogger("ml3error")


def _today_slot_timestamp(target: dt_time, now: float) -> float:
    dt = datetime.fromtimestamp(now)
    slot = datetime.combine(dt.date(), target)
    return slot.timestamp()


def _yesterday_slot_timestamp(target: dt_time, now: float) -> float:
    dt = datetime.fromtimestamp(now)
    yest = date.fromordinal(dt.date().toordinal() - 1)
    slot = datetime.combine(yest, target)
    return slot.timestamp()


_TOP_N = 10


def _fmt_rel(ts: float, now: float) -> str:
    diff = max(0.0, now - ts)
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h ago"
    return f"{int(diff // 86400)}d ago"


def _format_summary(
    project: str,
    now: float,
    since: float | None,
    fingerprints: list[dict],
    dropped: int,
    fails: int,
    suppressed: int,
    markup: str = "plain",
) -> tuple[str, str]:
    # Restrict to errors active in this window. If we have no
    # last_heartbeat yet, treat everything present as "this window".
    cutoff = since if since is not None else 0.0
    active = [
        fp for fp in fingerprints
        if fp["last_activity"] >= cutoff and not fp["resolved"]
    ]
    active.sort(key=lambda fp: fp["last_activity"], reverse=True)

    unique = len(active)

    if since is None:
        window = "so far"
    else:
        window_sec = max(0.0, now - since)
        if window_sec >= 86400:
            window = f"in the last {int(window_sec // 86400)}d"
        elif window_sec >= 3600:
            window = f"in the last {int(window_sec // 3600)}h"
        else:
            window = f"in the last {max(1, int(window_sec // 60))}m"

    # Subject only reports the *unique fingerprints active this window*.
    # We deliberately don't aggregate per-fp total_count into one number:
    # total_count is lifetime, so summing across recently-active rows
    # would overstate the window's activity for long-lived fingerprints.
    # Per-row totals below are still useful context.
    # Lead with an emoji that's easy to scan in a Telegram chat list:
    # ✅ when nothing fired, 🚨 when at least one error is active.
    icon = "✅" if unique == 0 else "🚨"
    noun = "error" if unique == 1 else "errors"
    subject = f"[{project}] {icon} ml3error digest — {unique} active {noun} {window}"

    if markup == "html":
        # HTML-escape user-controlled content (project name, exception
        # type, paths, function names) so Telegram's parse_mode=html
        # doesn't treat e.g. `<module>` as an unclosed tag and reject
        # the whole message.
        from .transport import _esc_html as _esc

        head = (
            f"<b>{_esc(f'[{project}]')}</b> {icon} ml3error digest — "
            f"<b>{unique}</b> active {noun} {_esc(window)}"
        )
    else:
        _esc = lambda s: s
        head = subject

    lines: list[str] = [head, ""]
    if active:
        lines.append(f"Most recent ({min(_TOP_N, len(active))} of {unique}):")
        for fp in active[:_TOP_N]:
            loc = _esc(f"{fp['rel_path']}:{fp['func_name']}()")
            exc_type = _esc(fp["exc_type"])
            if markup == "html":
                line = (
                    f"  <b>{exc_type}</b>  <code>{loc}</code>  "
                    f"(last {_esc(_fmt_rel(fp['last_activity'], now))}, "
                    f"{fp['total_count']}× total)"
                )
            else:
                line = (
                    f"  {exc_type}  {loc}  "
                    f"(last {_fmt_rel(fp['last_activity'], now)}, "
                    f"{fp['total_count']}× total)"
                )
            lines.append(line)
    else:
        lines.append("No errors active in this window. ✓")

    if fails or dropped:
        lines.append("")
        lines.append(
            f"Library: {fails} transport failure(s), {dropped} dropped from queue."
        )

    # Subject is always plain (used as email Subject only); body picks up markup.
    return subject, "\n".join(lines)


class Heartbeat:
    def __init__(
        self,
        project: str,
        heartbeat_time: dt_time,
        store: Store,
        transport: Transport,
        lock: threading.Lock,
    ):
        self._project = project
        self._target = heartbeat_time
        self._store = store
        self._transport = transport
        self._lock = lock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        t = threading.Thread(target=self._run, daemon=True, name="ml3error-heartbeat")
        t.start()
        self._thread = t

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _should_fire(self, now: float) -> bool:
        with self._lock:
            last = self._store.get_last_heartbeat()
        today_slot = _today_slot_timestamp(self._target, now)
        if now < today_slot:
            return False
        if last is None:
            return True
        return last < today_slot

    def fire(self, *, update_state: bool = True, now: float | None = None) -> bool:
        """Send a digest. Returns True on successful send.

        With update_state=True (default, used by the scheduled thread):
        advances last_heartbeat and subtracts the reported counters.

        With update_state=False (used by the CLI preview): sends the
        same message but leaves state untouched, so the scheduled
        firing still happens normally.
        """
        if now is None:
            now = time.time()
        with self._lock:
            dropped, fails, suppressed = self._store.read_counters()
            since = self._store.get_last_heartbeat()
            fingerprints = self._store.list_fingerprints(resolved=False)
        subject, body = _format_summary(
            self._project, now, since, fingerprints, dropped, fails, suppressed,
            markup=getattr(self._transport, "markup", "plain"),
        )
        ok = False
        try:
            ok = self._transport.send(subject, body)
        except Exception:
            log.warning("heartbeat send raised", exc_info=True)

        if not update_state:
            return ok

        today_slot = _today_slot_timestamp(self._target, now)
        with self._lock:
            self._store.set_last_heartbeat(today_slot)
            if ok:
                # Subtract the snapshot (not reset) so counters bumped
                # by other threads between snapshot and here survive into
                # the next heartbeat rather than being silently lost.
                self._store.subtract_counters(dropped, fails, suppressed)
            else:
                self._store.bump_transport_failures()
        return ok

    def _run(self) -> None:
        # Poll loop (not sleep-until-scheduled) because time.sleep across
        # a laptop suspend or VM pause is unreliable — it can wake up
        # hours late or fire immediately on resume. Re-checking the wall
        # clock every 5 minutes costs nothing and is correct by construction.
        while not self._stop.is_set():
            try:
                now = time.time()
                if self._should_fire(now):
                    self.fire(now=now)
            except Exception:
                log.warning("heartbeat loop tick failed", exc_info=True)
            self._stop.wait(constants.HEARTBEAT_POLL_SECONDS)
