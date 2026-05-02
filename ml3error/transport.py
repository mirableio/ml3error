from __future__ import annotations

import logging
import re
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
    # True if this fingerprint was marked resolved before the current
    # occurrence — surfaces as a "regression" banner in the message.
    regression: bool = False


def build_payload(
    project: str,
    fp: tuple[str, str, str],
    exc: BaseException,
    tb: TracebackType | None,
    first_seen: float,
    suppressed_count: int,
    with_locals: bool,
    regression: bool = False,
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
        regression=regression,
    )


def build_log_payload(
    project: str,
    fp: tuple[str, str, str],
    record: logging.LogRecord,
    first_seen: float,
    suppressed_count: int,
    regression: bool = False,
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
        regression=regression,
    )


def _format_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _esc_html(s: str) -> str:
    """Minimal HTML escaping for Telegram parse_mode=HTML.

    Telegram only requires escaping `<`, `>`, and `&` in body text; tag
    contents (inside <pre>, <code>, <b>, <i>) follow the same rules. We
    don't need full HTML escaping (no entity attributes are used).
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_HTML_TAGS = ("pre", "code", "b", "i")
_TAG_RE = re.compile(r"<(/?)(pre|code|b|i)>")
_PARTIAL_TAG_RE = re.compile(r"<[^>]*$")


def _close_dangling_html(body: str) -> str:
    """Make a (possibly mid-tag-truncated) HTML body well-formed.

    1. Strip any trailing partial tag like `...<pr` or `...<b`.
    2. Walk the body's open/close events for the tags we emit and
       append closes for any still-open ones in LIFO order — so
       `<b><i>x` becomes `<b><i>x</i></b>`, not `<b><i>x</b></i>`.

    Telegram's parse_mode=HTML rejects the whole message if it sees an
    unclosed (or improperly-nested) `<pre>`/`<code>`/`<b>`/`<i>`, so
    truncation must leave well-formed markup behind.
    """
    # Drop a partial open tag that the truncation cut in the middle.
    body = _PARTIAL_TAG_RE.sub("", body)

    stack: list[str] = []
    for match in _TAG_RE.finditer(body):
        is_close, name = match.group(1), match.group(2)
        if is_close:
            # Stray close (no matching open in our generated content,
            # so this branch is mostly defensive).
            if stack and stack[-1] == name:
                stack.pop()
        else:
            stack.append(name)

    for name in reversed(stack):
        body += f"</{name}>"
    return body


def render(
    payload: Payload,
    *,
    cooldown_hours: float,
    markup: str = "plain",
) -> tuple[str, str]:
    """Return (subject, body) with scrubbing, formatting, and size cap.

    `markup="plain"` produces an unformatted UTF-8 message suitable for
    email and Slack. `markup="html"` produces a Telegram-compatible HTML
    body with bold/italic/code/pre markers around appropriate parts. The
    `subject` returned is always plain (used as the email Subject header
    only — Telegram and Slack put everything in the body).
    """
    _, rel_path, func = payload.fp
    plain_subject = f"[{payload.project}] {payload.exc_type}: {payload.exc_message}"

    if markup == "html":
        esc = _esc_html
        bold = lambda s: f"<b>{s}</b>"
        italic = lambda s: f"<i>{s}</i>"
        code = lambda s: f"<code>{s}</code>"
        pre = lambda s: f"<pre>{s}</pre>"
        # Subject line in body is bold-tagged + escaped.
        head = (
            f"{bold(esc(f'[{payload.project}]'))} "
            f"{bold(esc(payload.exc_type))}: {esc(payload.exc_message)}"
        )
    else:
        esc = lambda s: s
        bold = lambda s: s
        italic = lambda s: s
        code = lambda s: s
        pre = lambda s: s
        head = plain_subject

    lines: list[str] = [head]

    if payload.regression:
        lines.append("")
        lines.append(bold(esc("⚠️ REOPENED — was marked resolved before this occurrence")))

    lines.append("")
    lines.append(f"at {code(esc(f'{rel_path} in {func}()'))}")
    lines.append(f"first seen: {esc(_format_ts(payload.first_seen))}")

    if payload.suppressed_count > 0:
        lines.append(
            italic(
                esc(
                    f"suppressed {payload.suppressed_count} similar errors "
                    f"in the last {cooldown_hours:g}h"
                )
            )
        )

    if payload.traceback_text:
        lines.append("")
        lines.append(pre(esc(payload.traceback_text.rstrip())))

    if payload.frame_locals:
        lines.append("")
        lines.append("locals:")
        local_lines: list[str] = []
        for label, locs in payload.frame_locals:
            local_lines.append(f"  {label}")
            if not locs:
                local_lines.append("    (no locals)")
                continue
            for name, val in locs.items():
                local_lines.append(f"    {name} = {val}")
        lines.append(pre(esc("\n".join(local_lines))))

    body = "\n".join(lines)
    if len(body) > constants.MAX_MESSAGE_CHARS:
        marker = "\n…[truncated]"
        body = body[: constants.MAX_MESSAGE_CHARS - len(marker)]
        if markup == "html":
            body = _close_dangling_html(body)
        body += marker

    # Email subjects stay plain regardless of markup.
    subject_line = plain_subject.split("\n", 1)[0]
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
        # Telegram supports HTML formatting via parse_mode. Default it on
        # so render(markup="html") output renders bold/italic/code/pre.
        # `notifiers` validates this against ['markdown', 'html'] so the
        # value must be lowercase. User config wins if explicit.
        if name == "telegram":
            self._base_config.setdefault("parse_mode", "html")

    @property
    def markup(self) -> str:
        """Which markup style render() should produce for this transport."""
        if self._name == "telegram" and (
            self._base_config.get("parse_mode") or ""
        ).lower() == "html":
            return "html"
        return "plain"

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
