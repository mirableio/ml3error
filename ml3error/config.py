from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import time as dt_time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

VALID_TRANSPORTS = ("email", "telegram", "slack")
VALID_HOOKS = ("excepthook", "threading", "asyncio")
ENV_PREFIX = "ML3ERROR_"
_SENTINEL = object()


@dataclass(frozen=True)
class Config:
    transport: str
    transport_config: dict[str, Any]
    state: str
    project: str
    project_root: Path
    cooldown_hours: float
    with_locals: bool
    heartbeat: bool
    heartbeat_time: dt_time
    hooks: tuple[str, ...] = ()


class ConfigError(ValueError):
    pass


def _infer_project_root() -> Path:
    main = sys.modules.get("__main__")
    main_file = getattr(main, "__file__", None)
    if not main_file:
        raise ConfigError(
            "Could not infer project_root: __main__ has no __file__ "
            "(REPL, Jupyter, or embedded interpreter). "
            "Pass project_root=Path(__file__).parent to init()."
        )
    path = Path(main_file).resolve().parent
    # Reject locations where the inferred root is wrong in practice:
    # site-packages → console-script / pipx entry points; a bin/ dir →
    # gunicorn/uvicorn-style launchers whose __main__ is the wrapper.
    # Loud failure beats silently writing state into venv/ or redacting
    # all paths to their basename.
    if "site-packages" in path.parts:
        raise ConfigError(
            f"Inferred project_root {path} is inside site-packages. "
            "Pass project_root=Path(__file__).parent from your entry module."
        )
    if path.name == "bin":
        raise ConfigError(
            f"Inferred project_root {path} is a bin/ directory "
            "(Gunicorn, Uvicorn, or similar launcher). "
            "Pass project_root=Path(__file__).parent from your entry module."
        )
    return path


def _parse_bool(s: str) -> bool:
    s = s.strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    raise ConfigError(f"Expected a boolean, got {s!r}")


def _parse_list(s: str) -> list[str]:
    if not s.strip():
        return []
    return [p.strip() for p in s.split(",") if p.strip()]


def _parse_time(s: str) -> dt_time:
    try:
        hh, mm = s.split(":", 1)
        return dt_time(int(hh), int(mm))
    except (ValueError, TypeError) as e:
        raise ConfigError(f"Expected HH:MM, got {s!r}") from e


def _env(key: str, default: Any = _SENTINEL) -> Any:
    v = os.environ.get(ENV_PREFIX + key)
    if v is None:
        return default
    return v


def _collect_transport_config_from_env() -> dict[str, Any]:
    """Pick up ML3ERROR_TRANSPORT_<KEY>=value pairs."""
    prefix = ENV_PREFIX + "TRANSPORT_"
    out: dict[str, Any] = {}
    for k, v in os.environ.items():
        if k.startswith(prefix) and k != ENV_PREFIX + "TRANSPORT":
            out[k[len(prefix):].lower()] = v
    return out


def build(**kwargs: Any) -> Config:
    """Merge init() kwargs with env vars and return an immutable Config."""
    # Pin to cwd/.env — without dotenv_path, python-dotenv walks up from
    # our own install location and can pick up unrelated files.
    load_dotenv(dotenv_path=".env", override=False)

    def pick(name: str, env_key: str, default: Any = _SENTINEL) -> Any:
        if name in kwargs and kwargs[name] is not None:
            return kwargs[name]
        env_val = _env(env_key, _SENTINEL)
        if env_val is not _SENTINEL:
            return env_val
        return default

    transport = pick("transport", "TRANSPORT")
    if transport is _SENTINEL:
        raise ConfigError("transport is required")
    if transport not in VALID_TRANSPORTS:
        raise ConfigError(
            f"transport must be one of {VALID_TRANSPORTS}, got {transport!r}"
        )

    transport_config = kwargs.get("transport_config")
    if transport_config is None:
        transport_config = _collect_transport_config_from_env()
    if not transport_config:
        raise ConfigError("transport_config is required")
    if not isinstance(transport_config, dict):
        raise ConfigError("transport_config must be a dict")

    project_root_in = pick("project_root", "PROJECT_ROOT", None)
    if project_root_in is None:
        project_root = _infer_project_root()
    else:
        project_root = Path(project_root_in).expanduser().resolve()

    project = pick("project", "PROJECT", None) or project_root.name

    state_in = pick("state", "STATE", None)
    if state_in is None:
        state = f"sqlite:///{project_root / '.ml3error.db'}"
    else:
        state = str(state_in)

    cooldown_raw = pick("cooldown_hours", "COOLDOWN_HOURS", 24)
    try:
        cooldown_hours = float(cooldown_raw)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"cooldown_hours must be a number, got {cooldown_raw!r}") from e

    with_locals_raw = pick("with_locals", "WITH_LOCALS", True)
    with_locals = with_locals_raw if isinstance(with_locals_raw, bool) else _parse_bool(str(with_locals_raw))

    heartbeat_raw = pick("heartbeat", "HEARTBEAT", True)
    heartbeat = heartbeat_raw if isinstance(heartbeat_raw, bool) else _parse_bool(str(heartbeat_raw))

    heartbeat_time_raw = pick("heartbeat_time", "HEARTBEAT_TIME", "09:00")
    if isinstance(heartbeat_time_raw, dt_time):
        heartbeat_time = heartbeat_time_raw
    else:
        heartbeat_time = _parse_time(str(heartbeat_time_raw))

    hooks_raw = pick("hooks", "HOOKS", ("excepthook", "threading"))
    if isinstance(hooks_raw, str):
        hooks_list = _parse_list(hooks_raw)
    else:
        hooks_list = list(hooks_raw)
    for h in hooks_list:
        if h not in VALID_HOOKS:
            raise ConfigError(f"Unknown hook {h!r}, valid: {VALID_HOOKS}")
    hooks = tuple(hooks_list)

    return Config(
        transport=transport,
        transport_config=dict(transport_config),
        state=state,
        project=str(project),
        project_root=project_root,
        cooldown_hours=cooldown_hours,
        with_locals=with_locals,
        heartbeat=heartbeat,
        heartbeat_time=heartbeat_time,
        hooks=hooks,
    )
