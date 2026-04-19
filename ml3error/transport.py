from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any

import notifiers

from . import constants
from .scrub import safe_repr, scrub


@dataclass
class Payload:
    """Everything needed to assemble and send one error notification."""

    project: str
    fp: tuple[str, str, str]  # (type, rel_path, func)
    exc_type: str
    exc_message: str
    traceback_text: str
    frame_locals: list[tuple[str, dict[str, str]]]  # [(frame_label, {name: safe_repr})]
    first_seen: float
    suppressed_count: int


def build_payload(
    project: str,
    fp: tuple[str, str, str],
    exc: BaseException,
    tb: TracebackType | None,
    first_seen: float,
    suppressed_count: int,
    with_locals: bool,
) -> Payload:
    exc_type = type(exc).__name__
    try:
        exc_message = scrub(str(exc))
    except Exception:
        exc_message = f"<unreprable {exc_type}>"

    tb_lines = traceback.format_exception(type(exc), exc, tb)
    traceback_text = scrub("".join(tb_lines))

    frame_locals: list[tuple[str, dict[str, str]]] = []
    if with_locals and tb is not None:
        cur: TracebackType | None = tb
        while cur is not None:
            code = cur.tb_frame.f_code
            label = f"{code.co_filename}:{cur.tb_lineno} in {code.co_name}"
            locs: dict[str, str] = {}
            for name, value in cur.tb_frame.f_locals.items():
                locs[name] = scrub(safe_repr(value))
            frame_locals.append((label, locs))
            cur = cur.tb_next

    return Payload(
        project=project,
        fp=fp,
        exc_type=exc_type,
        exc_message=exc_message,
        traceback_text=traceback_text,
        frame_locals=frame_locals,
        first_seen=first_seen,
        suppressed_count=suppressed_count,
    )


def build_log_payload(
    project: str,
    fp: tuple[str, str, str],
    record: logging.LogRecord,
    first_seen: float,
    suppressed_count: int,
) -> Payload:
    """Payload from a logging.LogRecord (no exc_info case).

    The fp's first element is the level name (e.g. 'ERROR'), so subject
    lines render as '[project] ERROR: <message>'. record.stack_info is
    included as traceback_text when present (typically only when the
    user passed stack_info=True to their log call). No frame locals.
    """
    level = fp[0]
    try:
        message = scrub(record.getMessage())
    except Exception:
        message = f"<unrenderable log message: {type(record.msg).__name__}>"
    stack = scrub(record.stack_info) if record.stack_info else ""
    return Payload(
        project=project,
        fp=fp,
        exc_type=level,
        exc_message=message,
        traceback_text=stack,
        frame_locals=[],
        first_seen=first_seen,
        suppressed_count=suppressed_count,
    )


def _format_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def render(payload: Payload, *, cooldown_hours: float) -> tuple[str, str]:
    """Return (subject, body) with scrubbing and size cap applied."""
    _, rel_path, func = payload.fp
    subject = f"[{payload.project}] {payload.exc_type}: {payload.exc_message}"

    lines = [
        subject,
        "",
        f"at {rel_path} in {func}()",
        f"first seen: {_format_ts(payload.first_seen)}",
    ]
    if payload.suppressed_count > 0:
        lines.append(
            f"suppressed {payload.suppressed_count} similar errors "
            f"in the last {cooldown_hours:g}h"
        )
    if payload.traceback_text:
        lines.append("")
        lines.append(payload.traceback_text.rstrip())

    if payload.frame_locals:
        lines.append("")
        lines.append("locals:")
        for label, locs in payload.frame_locals:
            lines.append(f"  {label}")
            if not locs:
                lines.append("    (no locals)")
                continue
            for name, val in locs.items():
                lines.append(f"    {name} = {val}")

    body = "\n".join(lines)
    if len(body) > constants.MAX_MESSAGE_CHARS:
        marker = "\n…[truncated]"
        body = body[: constants.MAX_MESSAGE_CHARS - len(marker)] + marker

    # Email subjects shouldn't be multi-line or massive.
    subject_line = subject.split("\n", 1)[0]
    if len(subject_line) > 200:
        subject_line = subject_line[:197] + "..."
    return subject_line, body


class Transport:
    def __init__(self, name: str, config: dict[str, Any]):
        self._name = name
        self._provider = notifiers.get_notifier(name)
        if self._provider is None:
            raise ValueError(f"Unknown notifiers provider: {name}")
        self._base_config = dict(config)

    def send(self, subject: str, body: str) -> bool:
        """Single send attempt. Returns True on success."""
        ok, _ = self.send_detailed(subject, body)
        return ok

    def send_detailed(self, subject: str, body: str) -> tuple[bool, str | None]:
        """Single send attempt. Returns (ok, error_reason). reason is None on success."""
        kwargs: dict[str, Any] = dict(self._base_config)
        if self._name == "email":
            kwargs.setdefault("subject", subject)
        kwargs["message"] = body or subject
        try:
            response = self._provider.notify(**kwargs)
            if response.errors:
                return False, "; ".join(str(e) for e in response.errors)
            return True, None
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def send_with_deadline(self, subject: str, body: str, deadline_seconds: float) -> bool:
        """Send with a wall-clock deadline.

        Used by the crash path where the process is about to exit and we
        must bound our total time. Python threads can't be cancelled, so
        on timeout we return False and let the daemon thread leak —
        acceptable since interpreter shutdown will tear it down anyway.
        """
        result: list[bool] = [False]

        def _run() -> None:
            try:
                result[0] = self.send(subject, body)
            except Exception:
                result[0] = False

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(deadline_seconds)
        if t.is_alive():
            return False
        return result[0]
