"""Tests for markdown_to_html() after the markdown-it-py migration."""

from __future__ import annotations

import pytest

from mindroom.matrix.message_builder import markdown_to_html

# --- Core bug fix: tables without blank lines ---


def test_table_without_preceding_blank_line() -> None:
    """Tables must parse even without a blank line before them."""
    html = markdown_to_html("Some text\n| A | B |\n| --- | --- |\n| 1 | 2 |")
    assert "<table>" in html
    assert "<td>1</td>" in html
    assert "| A |" not in html  # no raw pipe characters


def test_table_with_preceding_blank_line() -> None:
    """Tables with a blank line before them still work."""
    html = markdown_to_html("Some text\n\n| A | B |\n| --- | --- |\n| 1 | 2 |")
    assert "<table>" in html
    assert "<td>1</td>" in html


def test_multiple_tables() -> None:
    """Multiple tables separated by text both render."""
    md = "| A |\n| - |\n| 1 |\n\nText\n| B |\n| - |\n| 2 |"
    html = markdown_to_html(md)
    assert html.count("<table>") == 2


def test_table_after_heading() -> None:
    """Table immediately after a heading renders correctly."""
    html = markdown_to_html("## Results\n| K | V |\n| - | - |\n| a | b |")
    assert "<table>" in html
    assert "<h2>" in html


# --- nl2br replacement (breaks: True) ---


def test_newlines_become_br_in_paragraphs() -> None:
    """Single newlines inside paragraphs produce <br> tags."""
    html = markdown_to_html("Line 1\nLine 2\nLine 3")
    assert "<br" in html


def test_double_newline_creates_paragraphs() -> None:
    """Double newlines create separate paragraphs."""
    html = markdown_to_html("Para 1\n\nPara 2")
    assert html.count("<p>") == 2


# --- Fenced code blocks ---


def test_fenced_code_with_language() -> None:
    """Fenced code with a language tag gets Pygments highlighting."""
    html = markdown_to_html("```python\nprint('hi')\n```")
    assert "<pre>" in html
    assert "<code" in html
    # Pygments produces inline-style spans
    assert "style=" in html


def test_fenced_code_without_language() -> None:
    """Fenced code without a language tag renders as plain code."""
    html = markdown_to_html("```\nplain code\n```")
    assert "<pre>" in html
    assert "<code>" in html
    assert "plain code" in html


def test_fenced_code_unknown_language() -> None:
    """Unknown language falls back to plain escaped code."""
    html = markdown_to_html("```nosuchlang\nfoo\n```")
    assert "<pre>" in html
    assert "<code" in html
    assert "foo" in html


# --- Inline formatting ---


def test_bold() -> None:
    """Bold markdown renders as <strong>."""
    assert "<strong>bold</strong>" in markdown_to_html("**bold**")


def test_italic() -> None:
    """Italic markdown renders as <em>."""
    assert "<em>italic</em>" in markdown_to_html("*italic*")


def test_strikethrough() -> None:
    """Strikethrough markdown renders as <s>."""
    assert "<s>strike</s>" in markdown_to_html("~~strike~~")


def test_inline_code() -> None:
    """Backtick-delimited text renders as <code>."""
    assert "<code>code</code>" in markdown_to_html("`code`")


# --- Links, images, lists, blockquotes ---


def test_link() -> None:
    """Markdown links render as <a> tags."""
    html = markdown_to_html("[text](http://example.com)")
    assert '<a href="http://example.com">text</a>' in html


def test_image() -> None:
    """Markdown images render as <img> tags."""
    html = markdown_to_html("![alt](http://example.com/img.png)")
    assert "<img" in html
    assert 'alt="alt"' in html


def test_unordered_list() -> None:
    """Dash-prefixed items render as <ul>/<li>."""
    html = markdown_to_html("- a\n- b")
    assert "<ul>" in html
    assert "<li>" in html


def test_ordered_list() -> None:
    """Numbered items render as <ol>."""
    html = markdown_to_html("1. a\n2. b")
    assert "<ol>" in html


def test_blockquote() -> None:
    """Lines prefixed with > render as <blockquote>."""
    html = markdown_to_html("> quoted")
    assert "<blockquote>" in html


# --- HTML tag handling ---


def test_unsupported_tags_escaped() -> None:
    """Unknown HTML tags get entity-escaped."""
    html = markdown_to_html("<tool>content</tool>")
    assert "&lt;tool&gt;" in html
    assert "<tool>" not in html


def test_supported_tags_pass_through() -> None:
    """Known Matrix tags are preserved."""
    html = markdown_to_html("<code>example</code>")
    assert "<code>example</code>" in html


def test_mixed_known_unknown_tags() -> None:
    """Known and unknown tags in the same input are handled correctly."""
    html = markdown_to_html("<code>ok</code>\n<search>query</search>")
    assert "<code>ok</code>" in html
    assert "&lt;search&gt;" in html


# --- HTML block interaction (review findings 1 & 2) ---


def test_supported_block_html_followed_by_markdown() -> None:
    """Markdown after a supported block-level tag must still be parsed."""
    html = markdown_to_html("<div>ok</div>\n**bold**")
    assert "<strong>bold</strong>" in html
    assert "<div>ok</div>" in html


def test_unsupported_block_html_followed_by_markdown() -> None:
    """Markdown after an unsupported block-level tag must still be parsed."""
    html = markdown_to_html("<search>q</search>\n**bold**")
    assert "<strong>bold</strong>" in html
    assert "&lt;search&gt;" in html
    assert "<search>" not in html


# --- Edge cases from real agent output ---


def test_tool_marker_emoji_code_span() -> None:
    """V2 tool markers with emoji and backtick code spans render correctly."""
    html = markdown_to_html("\n\n\U0001f527 `search_web` [1] \u23f3\n")
    assert "<code>search_web</code>" in html
    assert "\U0001f527" in html


@pytest.mark.parametrize(
    "md",
    [
        "**bold** and *italic*",
        "text\n| H |\n| - |\n| v |",
        "```python\nx=1\n```",
        "> quote\n\ntext",
    ],
    ids=["inline", "table-no-blank", "code", "blockquote"],
)
def test_no_raw_markdown_leaks(md: str) -> None:
    """Rendered HTML should never contain raw markdown delimiters in output."""
    html = markdown_to_html(md)
    # Tables should not leak pipe characters outside tags
    if "| H |" in md:
        assert "| H |" not in html
