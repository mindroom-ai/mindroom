"""Focused contract tests for message_builder markdown boundary handling."""

from __future__ import annotations

from mindroom.matrix.message_builder import markdown_to_html


def test_same_line_div_followed_by_markdown_still_renders_markdown() -> None:
    """Single-line block HTML should not swallow following markdown."""
    html = markdown_to_html("<div>ok</div>\n**bold**")
    assert "<div>ok</div>" in html
    assert "<strong>bold</strong>" in html
    assert "**bold**" not in html
    assert "<p><div>" not in html


def test_same_line_div_followed_by_fence_still_renders_the_fence() -> None:
    """Single-line block HTML should not swallow a following fenced code block."""
    html = markdown_to_html("<div>ok</div>\n```\n<tool>\n```")
    assert "<div>ok</div>" in html
    assert "<pre><code>" in html
    assert "&lt;tool&gt;" in html
    assert "```" not in html


def test_raw_details_block_keeps_inner_markdown_literal() -> None:
    """Raw HTML blocks still follow literal CommonMark-style semantics."""
    html = markdown_to_html("<details>\n**bold**\n</details>")
    assert "<details>" in html
    assert "</details>" in html
    assert "**bold**" in html
    assert "<strong>bold</strong>" not in html


def test_raw_html_blockquote_in_list_item_keeps_inner_markdown_literal() -> None:
    """Nested raw HTML blocks are preserved but not reparsed for markdown."""
    html = markdown_to_html("- <blockquote>\n  **bold**\n  </blockquote>")
    assert "<ul>" in html
    assert "<li>" in html
    assert "<blockquote>" in html
    assert "</blockquote>" in html
    assert "**bold**" in html
    assert "<strong>bold</strong>" not in html


def test_nested_quote_list_div_keeps_inner_markdown_literal() -> None:
    """The renderer does not attempt generalized recovery inside nested containers."""
    html = markdown_to_html("- > <div>ok</div>\n  > **bold**")
    assert "<ul>" in html
    assert "<li>" in html
    assert "<blockquote>" in html
    assert "<div>ok</div>" in html
    assert "**bold**" in html
    assert "<strong>bold</strong>" not in html
