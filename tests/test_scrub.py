from ml3error.scrub import safe_repr, scrub


def test_email_is_redacted():
    out = scrub("contact dima@kuchin.net for help")
    assert "dima@kuchin.net" not in out
    assert "REDACTED" in out


def test_password_assignment_is_redacted():
    out = scrub("POST /login password=hunter2 returned 200")
    assert "hunter2" not in out


def test_bearer_token_is_redacted():
    out = scrub("Authorization: Bearer abc123XYZdef456GHI789")
    assert "abc123" not in out


def test_scrub_empty_is_noop():
    assert scrub("") == ""


def test_safe_repr_truncates():
    long = "x" * 500
    out = safe_repr(long)
    assert len(out) <= 200
    assert out.endswith("…")


def test_safe_repr_catches_raising_repr():
    class Bad:
        def __repr__(self):
            raise RuntimeError("boom")

    out = safe_repr(Bad())
    assert "unreprable" in out
    assert "Bad" in out


def test_safe_repr_short_value_not_truncated():
    assert safe_repr(42) == "42"


def test_long_filesystem_path_is_not_redacted():
    """Regression: the base64 pattern used to match [A-Za-z0-9+/]{40,}
    which swallowed filesystem paths into ***REDACTED***."""
    s = "loaded from /Users/alice/Work/Mirable/ml3error/ml3error/__init__.py"
    out = scrub(s)
    assert "REDACTED" not in out
    assert "/Users/alice/Work/Mirable/ml3error/ml3error/__init__.py" in out


def test_jwt_like_blob_is_still_redacted():
    # 40+ chars of letters/digits (no slashes) — the typical shape of a
    # JWT segment or long API token.
    s = "token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefg"
    out = scrub(s)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out
