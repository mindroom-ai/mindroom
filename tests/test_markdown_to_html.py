"""Tests for markdown_to_html() after the markdown-it-py migration."""

from __future__ import annotations

import pytest

from mindroom.matrix.message_builder import markdown_to_html
from mindroom.tool_system.events import ensure_visible_tool_marker_spacing

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


def test_tilde_fenced_code_does_not_double_escape_html() -> None:
    """Tilde-fenced code should preserve literal tags without double-escaping."""
    html = markdown_to_html("~~~\n<tool>test</tool>\n~~~")
    assert "<pre>" in html
    assert "<code>" in html
    assert "&lt;tool&gt;test&lt;/tool&gt;" in html
    assert "&amp;lt;tool&amp;gt;" not in html


def test_unclosed_fenced_code_is_not_mangled() -> None:
    """Unclosed fences should still preserve the remaining content as code."""
    html = markdown_to_html("```python\n<tool>code\nmore code")
    assert "<pre>" in html
    assert "<code" in html
    assert "tool" in html
    assert "code" in html
    assert "&amp;lt;tool&amp;gt;" not in html


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


def test_inline_code_escapes_html_without_double_escaping() -> None:
    """Pre-escaping must not corrupt literal tags inside inline code spans."""
    html = markdown_to_html("`<tool>`")
    assert "<code>&lt;tool&gt;</code>" in html
    assert "&amp;lt;tool&amp;gt;" not in html


def test_inline_math_preserves_latex_escapes() -> None:
    """Inline math content should survive markdown parsing verbatim."""
    html = markdown_to_html(r"$\text{previous\_speaker}$")
    assert r"$\text{previous\_speaker}$" in html
    assert r"$\text{previous_speaker}$" not in html


def test_display_math_preserves_latex_escapes() -> None:
    """Display math blocks should preserve escaped underscores."""
    html = markdown_to_html("$$\n\\text{previous\\_speaker} = \\text{Latone}\n$$")
    assert "$$\n\\text{previous\\_speaker} = \\text{Latone}\n$$" in html
    assert r"\text{previous_speaker}" not in html


def test_mixed_markdown_around_math_preserves_formatting() -> None:
    """Normal markdown should still render around protected math spans."""
    html = markdown_to_html(
        "Paragraph text with **bold text**, `inline_code`, and "
        r"$\text{previous\_speaker}$"
        "\n- bullet item",
    )
    assert "<strong>bold text</strong>" in html
    assert "<code>inline_code</code>" in html
    assert r"$\text{previous\_speaker}$" in html
    assert "<ul>" in html
    assert "<li>bullet item</li>" in html


def test_code_spans_containing_dollar_delimiters_remain_untouched() -> None:
    """Math protection must not rewrite code spans."""
    html = markdown_to_html(r"`$\text{previous\_speaker}$`")
    assert r"<code>$\text{previous\_speaker}$</code>" in html


def test_non_math_markdown_escaping_behavior_is_unchanged() -> None:
    """Escapes outside math should keep current markdown semantics."""
    html = markdown_to_html(r"outside \_ math")
    assert "<p>outside _ math</p>" in html


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


def test_matrix_specific_attributes_are_preserved() -> None:
    """Matrix-safe attributes and URL schemes should survive sanitization."""
    html = markdown_to_html(
        '<font data-mx-color="#00ff00">ok</font>'
        '<img src="mxc://matrix.org/abc123" alt="img">'
        '<span data-mx-bg-color="#001122" data-mx-color="#00ff00" data-mx-spoiler="reason" data-mx-maths="x^2">ok</span>'
        '<ol start="3"><li>third</li></ol>',
    )
    assert 'data-mx-color="#00ff00"' in html
    assert 'src="mxc://matrix.org/abc123"' in html
    assert 'data-mx-bg-color="#001122"' in html
    assert 'data-mx-spoiler="reason"' in html
    assert 'data-mx-maths="x^2"' in html
    assert '<ol start="3">' in html


def test_class_is_restricted_to_code_and_pre() -> None:
    """Only code/pre should retain class attributes."""
    html = markdown_to_html(
        '<span class="spoiler">secret</span><pre class="language-python"><code class="language-python">x=1</code></pre>',
    )
    assert "<span>secret</span>" in html
    assert '<pre class="language-python">' in html
    assert '<code class="language-python">x=1</code>' in html


def test_unterminated_supported_tag_fragment_is_preserved_as_text() -> None:
    """Malformed supported tag fragments should not disappear."""
    html = markdown_to_html('<div class="x"')
    assert html
    assert "&lt;div class=" in html
    assert '<div class="x"' not in html


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
    assert "<p><div>" not in html


def test_supported_block_html_followed_by_markdown_with_crlf() -> None:
    """CRLF line endings should not break supported block HTML handling."""
    html = markdown_to_html("<div>ok</div>\r\n**bold**")
    assert "<div>ok</div>" in html
    assert "<strong>bold</strong>" in html


def test_supported_block_html_followed_by_indented_markdown() -> None:
    """Indented markdown after block HTML should still be parsed."""
    html = markdown_to_html("<div>ok</div>\n  **bold**")
    assert "<div>ok</div>" in html
    assert "<strong>bold</strong>" in html


def test_unsupported_block_html_followed_by_markdown() -> None:
    """Markdown after an unsupported block-level tag must still be parsed."""
    html = markdown_to_html("<search>q</search>\n**bold**")
    assert "<strong>bold</strong>" in html
    assert "&lt;search&gt;" in html
    assert "<search>" not in html


def test_void_block_html_followed_by_markdown() -> None:
    """Void block tags should also get the CommonMark boundary fix."""
    html = markdown_to_html("<hr>\n**bold**")
    assert "<hr>" in html
    assert "<strong>bold</strong>" in html


def test_fenced_code_is_not_rewritten_by_html_preprocessing() -> None:
    """Fence contents should not pick up escaping or boundary rewrites."""
    html = markdown_to_html("```\n<tool>\n</div>\n**bold**\n```")
    assert "<pre>" in html
    assert "&lt;tool&gt;" in html
    assert "&lt;/div&gt;\n**bold**" in html
    assert "&amp;lt;tool&amp;gt;" not in html
    assert "\n\n**bold**" not in html
    assert "<strong>bold</strong>" not in html


def test_indented_code_block_is_not_rewritten_by_html_preprocessing() -> None:
    """Indented code blocks should not be altered by prose-only preprocessing."""
    html = markdown_to_html("    <tool>\n    </div>\n    **bold**")
    assert "<pre><code>" in html
    assert "&lt;tool&gt;" in html
    assert "&lt;/div&gt;" in html
    assert "**bold**" in html
    assert "&amp;lt;tool&amp;gt;" not in html
    assert "<strong>bold</strong>" not in html


def test_block_html_before_fenced_code_keeps_the_fence() -> None:
    """Supported block HTML should not swallow a following fenced code block."""
    html = markdown_to_html("<div>ok</div>\n```\n<tool>\n```")
    assert "<div>ok</div>" in html
    assert "<pre>" in html
    assert "&lt;tool&gt;" in html


def test_unsafe_raw_html_attributes_are_stripped() -> None:
    """Supported tags keep only safe attributes after sanitization."""
    html = markdown_to_html(
        '<a href="javascript:alert(1)" onclick="alert(1)">click</a>\n<img src="javascript:alert(1)" onerror="alert(1)" alt="safe">',
    )
    assert "<a>click</a>" in html
    assert 'href="javascript:alert(1)"' not in html
    assert "onclick=" not in html
    assert "<img" in html
    assert 'alt="safe"' in html
    assert 'src="javascript:alert(1)"' not in html
    assert "onerror=" not in html


def test_data_uri_attributes_are_stripped() -> None:
    """Dangerous data: URIs should be removed from otherwise allowed tags."""
    html = markdown_to_html('<a href="data:text/html,<script>alert(1)</script>">click</a>')
    assert "<a>click</a>" in html
    assert 'href="data:' not in html


@pytest.mark.parametrize(
    ("raw_html", "expected_fragment", "forbidden_fragment"),
    [
        ('<span style="color: red">ok</span>', 'style="color: red"', None),
        ('<span style="width: expression(alert(1))">ok</span>', "<span>ok</span>", "expression"),
        ('<span style="background: url(javascript:alert(1))">ok</span>', "<span>ok</span>", "url("),
        ('<span style="color: red; width: expression(x)">ok</span>', 'style="color: red"', "expression"),
        ('<span style="position: absolute">ok</span>', "<span>ok</span>", 'style="position: absolute"'),
    ],
    ids=[
        "style-allowed",
        "style-expression-blocked",
        "style-url-blocked",
        "style-multi-declaration-filtered",
        "style-unknown-property-dropped",
    ],
)
def test_style_sanitization(raw_html: str, expected_fragment: str, forbidden_fragment: str | None) -> None:
    """Only safe CSS properties and values should survive sanitization."""
    html = markdown_to_html(raw_html)
    assert expected_fragment in html
    if forbidden_fragment is not None:
        assert forbidden_fragment not in html


def test_html_comments_are_escaped() -> None:
    """HTML comments should survive only as visible escaped text."""
    html = markdown_to_html("<!-- secret -->")
    assert "&lt;!-- secret --&gt;" in html
    assert "<!-- secret -->" not in html


def test_malformed_html_comment_does_not_swallow_following_markdown() -> None:
    """Unclosed comment openers should be escaped before markdown parsing."""
    html = markdown_to_html("<!-- secret\n**bold**")
    assert "&lt;!-- secret" in html
    assert "<strong>bold</strong>" in html


def test_cdata_is_escaped() -> None:
    """CDATA-like declarations should survive only as literal text."""
    html = markdown_to_html("<![CDATA[secret]]>")
    assert "&lt;![CDATA[secret]]&gt;" in html


def test_entities_are_not_double_escaped() -> None:
    """Plain text entities should be escaped exactly once."""
    html = markdown_to_html("Tom & Jerry say 5 < 10")
    assert "&amp;" in html
    assert "&lt;" in html
    assert "&amp;amp;" not in html


def test_xhtml_self_closing_br_is_normalized() -> None:
    """Self-closing inline HTML tags should render as Matrix-safe HTML."""
    html = markdown_to_html("<br/>text")
    assert "<br>" in html
    assert "<br/>" not in html


def test_entity_encoded_javascript_scheme_is_stripped() -> None:
    """URL sanitization should decode entities before rejecting schemes."""
    html = markdown_to_html('<a href="&#106;avascript:alert(1)">click</a>')
    assert "<a>click</a>" in html
    assert "javascript" not in html


# --- Edge cases from real agent output ---


def test_tool_marker_emoji_code_span() -> None:
    """V2 tool markers with emoji and backtick code spans render correctly."""
    html = markdown_to_html("\n\n\U0001f527 `search_web` [1] \u23f3\n")
    assert "<code>search_web</code>" in html
    assert "\U0001f527" in html


def test_tool_marker_followed_by_thematic_break_does_not_render_as_setext_heading() -> None:
    """ISSUE-195: tool placeholder + `---` must render as <p>+<hr>, NOT <h2>."""
    html = markdown_to_html(ensure_visible_tool_marker_spacing("🔧 `tool` [1]\n---\n"))
    assert "<p>🔧 <code>tool</code> [1]</p>" in html
    assert "<hr>" in html
    assert "<h2>" not in html


def test_setext_h1_is_preserved_for_normal_markdown() -> None:
    """Normal CommonMark setext h1 syntax should keep rendering as a heading."""
    html = markdown_to_html("Heading\n===\n")
    assert "<h1>Heading</h1>" in html


def test_setext_h2_is_preserved_for_normal_markdown() -> None:
    """Normal CommonMark setext h2 syntax should keep rendering as a heading."""
    html = markdown_to_html("Heading\n---\n")
    assert "<h2>Heading</h2>" in html


@pytest.mark.parametrize(
    ("md", "forbidden_markers", "required_html"),
    [
        ("**bold** and *italic*", ("**bold**", "*italic*"), "<strong>bold</strong>"),
        ("text\n| H |\n| - |\n| v |", ("| H |",), "<table>"),
        ("```python\nx=1\n```", ("```python", "```"), "<pre>"),
        ("~~~\nx=1\n~~~", ("~~~",), "<pre>"),
        ("## Heading", ("## Heading",), "<h2>"),
        ("- item", ("- item",), "<ul>"),
        ("> quote\n\ntext", ("> quote",), "<blockquote>"),
    ],
    ids=["inline", "table-no-blank", "code", "tilde-code", "heading", "list", "blockquote"],
)
def test_no_raw_markdown_leaks(md: str, forbidden_markers: tuple[str, ...], required_html: str) -> None:
    """Rendered HTML should never contain raw markdown delimiters in output."""
    html = markdown_to_html(md)
    for marker in forbidden_markers:
        assert marker not in html
    assert required_html in html
