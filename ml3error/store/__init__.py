from __future__ import annotations

import os
from urllib.parse import urlparse

from .base import CrashStore, Store


def _sqlite_path(url: str) -> str:
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///") :]
    else:
        path = urlparse(url).path
    return os.path.expanduser(path)


def open_store(url: str) -> tuple[Store, CrashStore]:
    """Return (main_store, crash_store) for the given state URL."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme == "sqlite":
        from .sqlite import SQLiteCrashStore, SQLiteStore

        path = _sqlite_path(url)
        return SQLiteStore(path), SQLiteCrashStore(path)
    if scheme in ("redis", "rediss"):
        from .redis import RedisCrashStore, RedisStore

        return RedisStore(url), RedisCrashStore(url)
    raise ValueError(f"Unknown state scheme {scheme!r}; use sqlite:/// or redis://")

