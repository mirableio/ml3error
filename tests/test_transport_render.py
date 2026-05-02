from __future__ import annotations

from ml3error.transport import Payload, Transport, render


def _payload(**overrides) -> Payload:
    base = dict(
        project="my-bot",
        fp=("ValueError", "app.py", "main"),
        exc_type="ValueError",
        exc_message="something <bad> & other",  # contains HTML-special chars
        traceback_text='Traceback:\n  File "x.py" in main\n    raise ValueError("x")',
        frame_locals=[],
        first_seen=1_700_000_000.0,
        suppressed_count=0,
        regression=False,
    )
    base.update(overrides)
    return Payload(**base)


def test_render_plain_default():
    subj, body = render(_payload(), cooldown_hours=24)
    assert subj == "[my-bot] ValueError: something <bad> & other"
    # No HTML tags in plain mode.
    assert "<b>" not in body and "<pre>" not in body
    # User content not escaped.
    assert "<bad>" in body


def test_render_html_escapes_user_content_and_tags_known_parts():
    subj, body = render(_payload(), cooldown_hours=24, markup="html")
    # Subject (plain) is unchanged for email use.
    assert subj == "[my-bot] ValueError: something <bad> & other"
    # Body has the project bolded with HTML.
    assert "<b>[my-bot]</b>" in body
    # Exception type is bolded.
    assert "<b>ValueError</b>" in body
    # User content is HTML-escaped — no raw <bad> in body.
    assert "<bad>" not in body
    assert "&lt;bad&gt;" in body
    # Traceback wrapped in <pre>.
    assert "<pre>" in body and "</pre>" in body


def test_render_regression_banner_html():
    _, body = render(_payload(regression=True), cooldown_hours=24, markup="html")
    assert "<b>⚠️ REOPENED" in body


def test_render_regression_banner_plain():
    _, body = render(_payload(regression=True), cooldown_hours=24)
    assert "⚠️ REOPENED" in body
    # No HTML tags in plain mode.
    assert "<b>" not in body


def test_render_suppressed_line_italic_in_html():
    _, body = render(_payload(suppressed_count=6), cooldown_hours=24, markup="html")
    assert "<i>suppressed 6 similar errors in the last 24h</i>" in body


def test_transport_telegram_defaults_to_html_markup():
    # Construct without making a real notifier call — the markup property
    # only depends on _name and _base_config. We don't need notifiers to
    # actually be importable for this; Transport.__init__ does a real
    # `notifiers.get_notifier` call, so use a real provider name.
    t = Transport("telegram", {"token": "x", "chat_id": "y"})
    assert t.markup == "html"
    assert t._base_config.get("parse_mode") == "html"


def test_transport_email_uses_plain_markup():
    t = Transport("email", {"to": "a@example.com", "from": "b@example.com",
                            "host": "localhost", "port": 25})
    assert t.markup == "plain"


def test_transport_telegram_user_can_disable_html():
    t = Transport("telegram", {"token": "x", "chat_id": "y", "parse_mode": ""})
    assert t.markup == "plain"


def test_render_html_truncation_keeps_tags_balanced():
    """Long traceback wrapped in <pre> would produce malformed HTML if
    the truncation happened inside the tag — Telegram rejects that.
    Truncation must close any opened-but-not-closed tags."""
    huge_tb = "Traceback (most recent call last):\n" + ("  line of trace\n" * 500)
    p = _payload(traceback_text=huge_tb)
    _, body = render(p, cooldown_hours=24, markup="html")
    # Length is bounded.
    from ml3error import constants
    # Extra slack for closing tags + marker.
    assert len(body) <= constants.MAX_MESSAGE_CHARS + 60
    # All tags we use are balanced.
    for tag in ("pre", "code", "b", "i"):
        assert body.count(f"<{tag}>") == body.count(f"</{tag}>"), (
            f"unbalanced <{tag}>"
        )
    # No dangling angle bracket at the end of the markup.
    truncation_idx = body.rfind("…[truncated]")
    assert truncation_idx >= 0
    head = body[:truncation_idx]
    assert "<" not in head.split(">")[-1], "partial open tag at truncation point"


def test_close_dangling_html_appends_only_what_is_needed():
    from ml3error.transport import _close_dangling_html

    # Already balanced → no change.
    assert _close_dangling_html("<b>hi</b>") == "<b>hi</b>"
    # Open <pre>, no close → append closer.
    assert _close_dangling_html("<pre>oops") == "<pre>oops</pre>"
    # Truncated mid-tag → drop the partial then balance.
    assert _close_dangling_html("<pre>oops</pre><co") == "<pre>oops</pre>"
    # Nested opens close in LIFO order so the result is valid HTML.
    assert _close_dangling_html("<b><i>x") == "<b><i>x</i></b>"
    # Same nesting interrupted by truncated partial tag.
    assert _close_dangling_html("<b><i>x</i><pr") == "<b><i>x</i></b>"
