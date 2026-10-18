"""Tests for hook enrichment rendering."""

from __future__ import annotations

from mindroom.hooks import EnrichmentItem, render_enrichment_block, render_transient_context
from mindroom.hooks.enrichment import is_transient_context, strip_enrichment_blocks


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


def test_render_transient_context_wraps_nonempty_parts() -> None:
    """Transient wrapper should be recognizable without changing its body."""
    rendered = render_transient_context(("memory", "enrichment"))

    assert rendered == ("<mindroom_transient_context>\nmemory\n\nenrichment\n</mindroom_transient_context>")
    assert is_transient_context(rendered) is True
    assert render_transient_context(()) == ""


def test_strip_enrichment_blocks_removes_every_block() -> None:
    """Every rendered block goes, with the blank lines around it, and plain text is untouched."""
    block = render_enrichment_block([EnrichmentItem(key="location", text="latitude: 47.6")])
    text = f"When does Atlas ship?\n\n{block}"

    assert strip_enrichment_blocks(text) == "When does Atlas ship?"
    assert strip_enrichment_blocks(f"{block}\n\nhi\n\n{block}") == "hi"
    assert strip_enrichment_blocks("no context") == "no context"


def test_strip_enrichment_blocks_joins_the_text_around_a_block_with_one_blank_line() -> None:
    """A block between two pieces of text leaves exactly one blank line between them."""
    block = render_enrichment_block([EnrichmentItem(key="location", text="latitude: 47.6")])

    assert strip_enrichment_blocks(f"a\n\n{block}\n\nb") == "a\n\nb"
    assert strip_enrichment_blocks(f"a\n{block}\nb") == "a\n\nb"
    assert strip_enrichment_blocks(f"{block}\n\n    indented") == "    indented"
    assert strip_enrichment_blocks(f"  first\n\n{block}") == "  first"


def test_strip_enrichment_blocks_leaves_text_without_a_block_byte_identical() -> None:
    """Pasted code keeps its indentation and trailing newline when nothing was attached."""
    text = "  def f():\n      return 1\n\n"

    assert strip_enrichment_blocks(text) == text
    assert strip_enrichment_blocks("") == ""
