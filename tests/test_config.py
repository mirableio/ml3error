from datetime import time as dt_time
from pathlib import Path

import pytest

from ml3error import config


def _base(tmp_path: Path, **extra):
    base = dict(
        transport="telegram",
        transport_config={"token": "x", "chat_id": "y"},
        project_root=tmp_path,
        heartbeat=False,
        hooks=[],
    )
    base.update(extra)
    return base


def test_build_defaults(tmp_path):
    cfg = config.build(**_base(tmp_path))
    assert cfg.transport == "telegram"
    assert cfg.project == tmp_path.name
    assert cfg.state.startswith("sqlite:///")
    assert cfg.cooldown_hours == 24.0
    assert cfg.with_locals is True
    assert cfg.heartbeat_time == dt_time(9, 0)


def test_missing_transport_raises(tmp_path):
    with pytest.raises(config.ConfigError):
        config.build(project_root=tmp_path, transport_config={"t": "v"})


def test_unknown_transport_raises(tmp_path):
    with pytest.raises(config.ConfigError):
        config.build(transport="signal", transport_config={"x": 1}, project_root=tmp_path)


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ML3ERROR_COOLDOWN_HOURS", "1.5")
    monkeypatch.setenv("ML3ERROR_HEARTBEAT_TIME", "07:30")
    cfg = config.build(**_base(tmp_path))
    assert cfg.cooldown_hours == 1.5
    assert cfg.heartbeat_time == dt_time(7, 30)


def test_kwargs_win_over_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ML3ERROR_COOLDOWN_HOURS", "1")
    cfg = config.build(**_base(tmp_path, cooldown_hours=12))
    assert cfg.cooldown_hours == 12.0


def test_hooks_from_env_comma(monkeypatch, tmp_path):
    monkeypatch.setenv("ML3ERROR_HOOKS", "excepthook, asyncio")
    base = _base(tmp_path)
    base.pop("hooks")
    cfg = config.build(**base)
    assert cfg.hooks == ("excepthook", "asyncio")


def test_invalid_hook_raises(tmp_path):
    with pytest.raises(config.ConfigError):
        config.build(**_base(tmp_path, hooks=["nope"]))


def test_site_packages_rejected(tmp_path):
    import sys
    fake = tmp_path / "venv" / "lib" / "python3.10" / "site-packages"
    fake.mkdir(parents=True)
    orig_main = sys.modules.get("__main__")
    sys.modules["__main__"] = type("M", (), {"__file__": str(fake / "entry.py")})()
    try:
        with pytest.raises(config.ConfigError):
            config.build(
                transport="telegram",
                transport_config={"token": "x"},
                heartbeat=False,
                hooks=[],
            )
    finally:
        if orig_main is not None:
            sys.modules["__main__"] = orig_main


def test_bin_dir_rejected(tmp_path):
    import sys
    fake = tmp_path / "venv" / "bin"
    fake.mkdir(parents=True)
    orig_main = sys.modules.get("__main__")
    sys.modules["__main__"] = type("M", (), {"__file__": str(fake / "gunicorn")})()
    try:
        with pytest.raises(config.ConfigError):
            config.build(
                transport="telegram",
                transport_config={"token": "x"},
                heartbeat=False,
                hooks=[],
            )
    finally:
        if orig_main is not None:
            sys.modules["__main__"] = orig_main
