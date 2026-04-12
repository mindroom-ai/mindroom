"""Tests for hook enrichment rendering."""

from __future__ import annotations

from mindroom.hooks import EnrichmentItem, render_enrichment_block


def test_render_enrichment_block_is_stable() -> None:
    """Rendering should be deterministic for one item set."""
    items = [
        EnrichmentItem(key="location", text="User is in Amsterdam", cache_policy="stable"),
        EnrichmentItem(key="weather", text="12C and windy", cache_policy="volatile"),
    ]

    rendered = render_enrichment_block(items)

    assert rendered == (
        "<mindroom_message_context>\n"
        '<item key="location" cache_policy="stable">\n'
        "User is in Amsterdam\n"
        "</item>\n"
        '<item key="weather" cache_policy="volatile">\n'
        "12C and windy\n"
        "</item>\n"
        "</mindroom_message_context>"
    )


def test_render_enrichment_block_escapes_xml_sensitive_content() -> None:
    """Rendered enrichment should escape keys and text so the block stays well-formed."""
    rendered = render_enrichment_block(
        [
            EnrichmentItem(
                key='weather"<now>',
                text='Use <rain & wind> "carefully"',
            ),
        ],
    )

    assert rendered == (
        "<mindroom_message_context>\n"
        '<item key="weather&quot;&lt;now&gt;" cache_policy="volatile">\n'
        "Use &lt;rain &amp; wind&gt; &quot;carefully&quot;\n"
        "</item>\n"
        "</mindroom_message_context>"
    )
