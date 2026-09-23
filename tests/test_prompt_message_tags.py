"""Tests for model-facing Matrix ``<msg>`` tag rendering."""

from mindroom.prompt_message_tags import render_msg_tag


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
