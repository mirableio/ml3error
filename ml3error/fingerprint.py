from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from types import TracebackType


def _innermost(tb: TracebackType | None) -> TracebackType | None:
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    return tb


def _relpath(filename: str, project_root: Path) -> str:
    try:
        abs_path = Path(filename).resolve()
    except (OSError, ValueError):
        return os.path.basename(filename) or "<unknown>"
    try:
        return str(abs_path.relative_to(project_root))
    except ValueError:
        return abs_path.name or "<unknown>"


def compute(
    exc: BaseException,
    tb: TracebackType | None,
    project_root: Path,
) -> tuple[str, str, str]:
    """Return (type_name, rel_path, func_name) for the innermost frame.

    We use the innermost (actually-raising) frame rather than the
    outermost so fingerprint reflects the failure site, not the
    entry point — two different bugs reached from `main()` stay distinct.
    """
    type_name = type(exc).__name__
    innermost = _innermost(tb)
    if innermost is None:
        return (type_name, "<unknown>", "<unknown>")
    code = innermost.tb_frame.f_code
    return (type_name, _relpath(code.co_filename, project_root), code.co_name)


def compute_from_record(
    record: logging.LogRecord,
    project_root: Path,
) -> tuple[str, str, str]:
    """Fingerprint for a logging.LogRecord that has no exc_info.

    Uses (level, rel_path, func:lineno). Line number is embedded in the
    func slot so two distinct log calls in the same function don't
    collapse into one fingerprint — unlike the exception path (which
    excludes line numbers so minor code edits don't create new fps),
    log sites are discrete user intents and deserve per-line identity.
    """
    level = getattr(record, "levelname", "LOG") or "LOG"
    filename = getattr(record, "pathname", "") or ""
    rel = _relpath(filename, project_root) if filename else "<unknown>"
    func = getattr(record, "funcName", "") or "<unknown>"
    lineno = getattr(record, "lineno", 0) or 0
    return (level, rel, f"{func}:{lineno}")


def to_key(fp: tuple[str, str, str]) -> str:
    """Stable primary-key string for the fingerprint.

    SHA-1 (not cryptographic use — just a compact deterministic hash).
    The \\x1f separator is ASCII unit-separator, which won't appear in
    a filename or identifier and so can't be confused with part boundaries.
    """
    raw = "\x1f".join(fp).encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()
