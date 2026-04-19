from pathlib import Path

from ml3error import fingerprint


def _raise_and_capture():
    try:
        raise ValueError("hi")
    except ValueError as e:
        return e, e.__traceback__


def test_fingerprint_basic():
    exc, tb = _raise_and_capture()
    fp = fingerprint.compute(exc, tb, Path(__file__).resolve().parent)
    assert fp[0] == "ValueError"
    assert fp[1] == "test_fingerprint.py"
    assert fp[2] == "_raise_and_capture"


def test_fingerprint_key_is_stable():
    fp = ("ValueError", "app.py", "foo")
    assert fingerprint.to_key(fp) == fingerprint.to_key(fp)


def test_fingerprint_without_tb():
    exc = ValueError("hi")
    fp = fingerprint.compute(exc, None, Path.cwd())
    assert fp == ("ValueError", "<unknown>", "<unknown>")


def test_fingerprint_outside_project_root_falls_back_to_basename(tmp_path):
    exc, tb = _raise_and_capture()
    fp = fingerprint.compute(exc, tb, tmp_path)
    assert fp[1] == "test_fingerprint.py"
