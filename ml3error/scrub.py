from __future__ import annotations

from . import constants


def safe_repr(value: object) -> str:
    try:
        s = repr(value)
    except Exception:
        return f"<unreprable {type(value).__name__}>"
    if len(s) > constants.SAFE_REPR_MAX_CHARS:
        return s[: constants.SAFE_REPR_MAX_CHARS - 1] + "…"
    return s


def scrub(text: str) -> str:
    if not text:
        return text
    for pattern in constants.SCRUB_PATTERNS:
        text = pattern.sub(constants.REDACTED, text)
    return text
