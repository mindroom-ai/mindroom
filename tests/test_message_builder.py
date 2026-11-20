"""Focused contract tests for message_builder markdown boundary handling."""

from __future__ import annotations

import pytest

from mindroom.matrix.message_builder import (
    build_matrix_edit_content,
    build_message_content,
    build_reaction_content,
    build_thread_relation,
    markdown_to_html,
)


def test_build_thread_relation_returns_fallback_reply_relation() -> None:
    """Thread relation construction should be available without fake message content."""
    assert build_thread_relation("$thread", latest_thread_event_id="$latest") == {
        "rel_type": "m.thread",
        "event_id": "$thread",
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$latest"},
    }


def test_room_level_reply_names_its_target_without_a_client_quote() -> None:
    """A reply outside a thread keeps its target for MindRoom but gives clients no ``m.in_reply_to`` to quote."""
    content = build_message_content("Answer", reply_to_event_id="$prompt")

    assert content["m.relates_to"] == {"io.mindroom.in_reply_to": {"event_id": "$prompt"}}


def test_build_matrix_edit_content_wraps_replacement_content() -> None:
    """Matrix edit wrapping should be shared instead of hand-built at call sites."""
    replacement = {
        "msgtype": "io.mindroom.tool_approval",
        "body": "Expired: shell.run",
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
    }

    assert build_matrix_edit_content(event_id="$approval", new_content=replacement) == {
        "msgtype": "io.mindroom.tool_approval",
        "body": "Expired: shell.run",
        "m.new_content": {
            "msgtype": "io.mindroom.tool_approval",
            "body": "Expired: shell.run",
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval"},
    }


def test_build_reaction_content_returns_annotation_relation() -> None:
    """Reaction content construction should be shared at send sites."""
    assert build_reaction_content("$event", "✅") == {
        "m.relates_to": {
            "rel_type": "m.annotation",
            "event_id": "$event",
            "key": "✅",
        },
    }


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


@pytest.mark.parametrize(
    ("markdown", "alt"),
    [
        ("![logo](mxc://placeholder)", "logo"),
        ("![logo](./x.png)", "logo"),
        ("![chart](att_1)", "chart"),
        ("![](https://?q)", "image"),
        ("![s](mxc://s//)", "s"),
        ("![p](/home/user/plot.png)", "p"),
        ("![p](sandbox:/mnt/data/plot.png)", "p"),
        ('<img src="mxc://placeholder" alt="raw">', "raw"),
        ("![a](mxc://?/id)", "a"),
        ("![a](https://example.org:garbage/x)", "a"),
        ("<hr>\n![logo](./logo.png)", "logo"),
        ("![a](mxc://.../id)", "a"),
        ("![a](mxc://server/id?foo)", "a"),
        ("![a](mxc://server/id#x)", "a"),
        ("![a](https://example.org:99999/a)", "a"),
        ('<img src="./bad.png" src="https://example.org/x.png" alt="a">', "a"),
        ('<img src="./bad.png" src="https://example.org/x.png" alt="a" />', "a"),
    ],
)
def test_unloadable_image_becomes_a_note(markdown: str, alt: str) -> None:
    """An image whose source no client can load renders as a visible note, never as a broken <img>."""
    html = markdown_to_html(markdown)
    assert "<img" not in html
    assert f"⚠️ Image not sent: {alt} (no uploaded file)" in html


@pytest.mark.parametrize(
    "markdown",
    [
        "![logo](mxc://matrix.org/abc123)",
        "![logo](https://example.org/logo.png)",
        "![l](http://example.org:8080)",
        "![a](https://user@example.org/x.png)",
    ],
)
def test_loadable_image_is_kept(markdown: str) -> None:
    """Uploaded media and web images still render as <img>."""
    html = markdown_to_html(markdown)
    assert "<img" in html
    assert "Image not sent" not in html


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ('<img src="mxc://[2001:db8::1]:8448/id" alt="a">', '<img src="mxc://[2001:db8::1]:8448/id" alt="a">'),
        ('<img src="https://[2001:db8::1]/x.png" alt="a">', '<img src="https://[2001:db8::1]/x.png" alt="a">'),
        ("![a](mxc://[2001:db8::1]:8448/id)", "<p>⚠️ Image not sent: a (no uploaded file)</p>\n"),
        ("![a](https://[2001:db8::1]/x.png)", "<p>⚠️ Image not sent: a (no uploaded file)</p>\n"),
        ('<img src="https://a b/x" alt="a">', "⚠️ Image not sent: a (no uploaded file)"),
        ('<img src="mxc://server/a b" alt="a">', "⚠️ Image not sent: a (no uploaded file)"),
        (
            '<img src="https://example.org/Team Photo.png" alt="T">',
            '<img src="https://example.org/Team Photo.png" alt="T">',
        ),
        ('<img src="https://a\tb/x" alt="a">', "⚠️ Image not sent: a (no uploaded file)"),
        ('<img src=" https://example.org/x.png " alt="ok">', '<img src="https://example.org/x.png" alt="ok">'),
        ("![chart](mxc://example.org/diagram.png)", "<p>⚠️ Image not sent: chart (no uploaded file)</p>\n"),
        ("![logo](https://%65xample.org/logo.png)", '<p><img src="https://%65xample.org/logo.png" alt="logo"></p>\n'),
        ("![l](https://example.org/a%20b.png)", '<p><img src="https://example.org/a%20b.png" alt="l"></p>\n'),
        ('<a href="" href="https://example.org">Docs</a>', '<p><a href="https://example.org">Docs</a></p>\n'),
    ],
)
def test_the_checked_src_is_the_emitted_src(markdown: str, expected: str) -> None:
    """Raw bracketed IPv6 loads and is kept as written; markdown-it's ``%5B…%5D`` spelling and whitespace cannot load."""
    assert markdown_to_html(markdown) == expected


def test_quoted_image_beside_its_code_span_copy_leaves_the_code() -> None:
    """A real image and an identical code span in one quoted paragraph: no broken image, the code untouched."""
    html = markdown_to_html("> ![a\n> b](bad) ![a\n> b](bad)\n> `![a\nb](bad)`")
    assert "<code>![a b](bad)</code>" in html
    assert "<img" not in html


@pytest.mark.parametrize("close", [">", " />"])
def test_duplicate_src_keeps_only_the_first(close: str) -> None:
    """Browsers load the first ``src``; the sanitizer checks that one and emits only it."""
    html = markdown_to_html(f'<img src="https://example.org/x.png" src="./bad.png" alt="a"{close}')
    assert html.count("src=") == 1
    assert '<img src="https://example.org/x.png" alt="a">' in html


def test_image_syntax_in_code_stays_code() -> None:
    """Image syntax in code spans and fences is code, not an image or a note."""
    html = markdown_to_html("Use `![logo](mxc://placeholder)` here.\n\n```\n![logo](./x.png)\n```")
    assert "<code>![logo](mxc://placeholder)</code>" in html
    assert "![logo](./x.png)" in html
    assert "Image not sent" not in html


@pytest.mark.parametrize(
    ("markdown", "note"),
    [
        ("![![x](mxc://placeholder)](bad)", "⚠️ Image not sent: x (no uploaded file)"),
        ("![<b>x</b>](bad)", "⚠️ Image not sent: x (no uploaded file)"),
        ('<img src="bad" alt="<b>x</b>">', "⚠️ Image not sent: &lt;b&gt;x&lt;/b&gt; (no uploaded file)"),
    ],
)
def test_unloadable_image_alt_is_escaped_text(markdown: str, note: str) -> None:
    """The alt text goes into the note as escaped text, so neither Markdown nor HTML in it renders."""
    html = markdown_to_html(markdown)
    assert note in html
    assert "<img" not in html
    assert "<b>" not in html


def test_unloadable_image_inside_link_keeps_the_link() -> None:
    """A linked image with no usable source leaves the link around the note."""
    html = markdown_to_html("[![a](bad)](https://example.org)")
    assert '<a href="https://example.org">⚠️ Image not sent: a (no uploaded file)</a>' in html
