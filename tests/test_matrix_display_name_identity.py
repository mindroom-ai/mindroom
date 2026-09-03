"""Tests for Matrix display-name identity prompt rendering."""

from mindroom.prompt_message_tags import render_msg_tag
from mindroom.prompts import AGENT_IDENTITY_CONTEXT_TEMPLATE


def test_render_msg_tag_preserves_mxid_and_adds_display_name() -> None:
    """Display names supplement the canonical Matrix ID rather than replacing it."""
    rendered = render_msg_tag(
        sender="@zorquan-x:matrix.example.org",
        display_name="Banana Man",
        body="Hello",
        event_id="$event",
        ts="2026-09-03 14:00 EDT",
    )

    assert 'from="@zorquan-x:matrix.example.org"' in rendered
    assert 'display_name="Banana Man"' in rendered
    assert 'event_id="$event"' in rendered
    assert 'ts="2026-09-03 14:00 EDT"' in rendered
    assert "<![CDATA[Hello]]>" in rendered


def test_render_msg_tag_omits_display_name_when_unavailable() -> None:
    """Existing callers remain compatible when no display name is available."""
    rendered = render_msg_tag(
        sender="@alice:example.org",
        body="Hello",
    )

    assert 'from="@alice:example.org"' in rendered
    assert "display_name=" not in rendered


def test_matrix_prompt_defines_mxid_as_canonical_identity() -> None:
    """Identity guidance distinguishes stable Matrix IDs from mutable display names."""
    assert "canonical, stable identity" in AGENT_IDENTITY_CONTEXT_TEMPLATE
    assert "may change over time without changing who the person is" in AGENT_IDENTITY_CONTEXT_TEMPLATE
    assert "treat the newest display name as current" in AGENT_IDENTITY_CONTEXT_TEMPLATE
    assert "not evidence of a different person" in AGENT_IDENTITY_CONTEXT_TEMPLATE
