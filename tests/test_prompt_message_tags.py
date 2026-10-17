"""Tests for model-facing Matrix ``<msg>`` tag rendering and reading them back."""

from mindroom.prompt_message_tags import parse_msg_tags, render_msg_tag


def test_render_msg_tag_adds_display_name_next_to_canonical_matrix_id() -> None:
    """The display name is extra metadata; ``from`` stays the stable Matrix ID."""
    assert render_msg_tag(
        sender="@alice:example.org",
        body="Hello",
        event_id="$event",
        ts="2026-09-03 14:00 EDT",
        display_name='Banana "Man" & Co',
    ) == (
        '<msg event_id="$event" from="@alice:example.org" '
        "display_name='Banana \"Man\" &amp; Co' "
        'ts="2026-09-03 14:00 EDT"><![CDATA[Hello]]></msg>'
    )


def test_render_msg_tag_omits_display_name_when_unknown() -> None:
    """Senders without a cached display name render with the Matrix ID only."""
    assert render_msg_tag(sender="@alice:example.org", body="Hello") == (
        '<msg from="@alice:example.org"><![CDATA[Hello]]></msg>'
    )


def test_parse_msg_tags_reads_back_each_sender_and_body() -> None:
    """Rendered tags read back as written, a tag without a sender is the default sender's, and plain text is one body."""
    tagged = (
        "Header\n<messages>"
        + render_msg_tag(sender='@o"brien:x', body="Use a[b[0]]> c", event_id="$1", display_name="O'Brien")
        + "<msg><![CDATA[No sender]]></msg></messages>"
    )
    assert parse_msg_tags(tagged, default_sender="@alice:x") == [
        ('@o"brien:x', "Use a[b[0]]> c"),
        ("@alice:x", "No sender"),
    ]
    assert parse_msg_tags("<conversation>plain</conversation>", default_sender="@alice:x") == [("@alice:x", "plain")]
