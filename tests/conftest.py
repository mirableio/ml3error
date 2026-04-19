from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Isolate tests from the project's own .env + shell env.

    `config.build()` auto-loads .env from cwd on every call (via
    python-dotenv). We point cwd at a clean tmp dir so the project's
    .env isn't picked up, and also strip any ML3ERROR_* vars that may
    already be in the shell.
    """
    for key in list(os.environ):
        if key.startswith("ML3ERROR_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    yield


class FakeResponse:
    def __init__(self, errors=None):
        self.errors = errors


class FakeProvider:
    def __init__(self, sink: list):
        self._sink = sink
        self.errors_to_return = None

    def notify(self, **kwargs):
        self._sink.append(kwargs)
        return FakeResponse(errors=self.errors_to_return)


@pytest.fixture
def sent_messages():
    return []


@pytest.fixture
def fake_notifier(sent_messages):
    provider = FakeProvider(sent_messages)

    def factory(name):
        return provider

    with patch("notifiers.get_notifier", side_effect=factory):
        yield provider


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("pass\n")
    return tmp_path


@pytest.fixture
def fresh_ml3error(fake_notifier):
    """Import a fresh ml3error module per test so state doesn't leak."""
    mods = [m for m in list(sys.modules) if m.startswith("ml3error")]
    for m in mods:
        sys.modules.pop(m, None)
    import ml3error  # noqa: F401
    importlib.reload(sys.modules["ml3error"])
    yield sys.modules["ml3error"]
    try:
        sys.modules["ml3error"].close()
    except Exception:
        pass
