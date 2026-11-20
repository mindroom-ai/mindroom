"""Focused contract tests for message_builder markdown boundary handling."""

from __future__ import annotations

from mindroom.matrix.message_builder import (
    build_matrix_edit_content,
    build_message_content,
    build_reaction_content,
    build_thread_relation,
    markdown_to_html,
    replace_unsendable_images,
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


_NOTE = "\u26a0\ufe0f Image not sent: {} (no uploaded file)"


def test_placeholder_mxc_images_become_a_visible_note() -> None:
    """An mxc:// image without one server and one media id, the shape agents invent, is replaced with a note."""
    targets = ("mxc://placeholder", "mxc://...", "mxc://\u2026", "mxc://example.org/", "mxc://server//", "mxc://s/a/b")
    for target in targets:
        assert replace_unsendable_images(f"See ![logo]({target}) here.") == f"See {_NOTE.format('logo')} here."


def test_non_url_image_targets_become_a_visible_note() -> None:
    """A relative path, attachment id, absolute path, sandbox link, or http(s) URL with no host is no image."""
    text = "![a](./login.png)\n\n- ![b](att_123)\n\n> ![c](/home/me/x.png) and ![d](sandbox:/x.png)"
    assert replace_unsendable_images(text) == (
        f"{_NOTE.format('a')}\n\n- {_NOTE.format('b')}\n\n> {_NOTE.format('c')} and {_NOTE.format('d')}"
    )
    for target in ("https://?q", "https://#top", "https:///a.png", "http://@/a.png"):
        assert replace_unsendable_images(f"![x]({target})") == _NOTE.format("x")


def test_uploaded_mxc_images_are_kept() -> None:
    """An image of a complete mxc://server/media_id URI stays as written."""
    text = "# Logo\n\n![logo](mxc://mindroom.chat/AbC123)"
    assert replace_unsendable_images(text) == text


def test_http_images_are_kept() -> None:
    """An http(s) image with a host stays as written, with or without user, port, query, or fragment."""
    text = (
        "| a | b |\n| - | - |\n| ![x](https://example.org/a.png) | ![y](http://example.org/b.png) |\n\n"
        "![z](https://user@example.org:8443/c.png?size=2#top) ![w](HTTPS://example.org)"
    )
    assert replace_unsendable_images(text) == text


def test_images_in_code_are_kept() -> None:
    """Image syntax inside a code span or fence is text; only the real image in its own block is replaced."""
    text = "`![x](att_1)` is code.\n\n```md\n![x](att_1)\n```\n\n    ![x](att_1)\n\n![x](att_1)"
    assert replace_unsendable_images(text) == text.removesuffix("![x](att_1)") + _NOTE.format("x")


def test_a_source_that_also_appears_as_code_in_its_block_is_left_alone() -> None:
    """When the image's source also occurs as code in the same block, the mapping is unsure, so nothing changes."""
    for text in ("`![x](att_1)` then ![x](att_1)", "| `![x](att_1)` | ![x](att_1) |\n| - | - |"):
        assert replace_unsendable_images(text) == text


def test_sources_the_parser_rewrote_never_reach_later_code() -> None:
    """An escaped table cell or a quoted multiline image cannot be found verbatim, so later code stays as written."""
    escaped_cell = "| ![a\\|b](bad) |\n| --- |\n\n`![a|b](bad)`"
    quoted = "> ![x\n> y](bad)\n\n```\n![x\ny](bad)\n```"
    for text in (escaped_cell, quoted):
        assert replace_unsendable_images(text) == text


def test_lone_carriage_returns_separate_the_images() -> None:
    """Line endings are normalized first, so two images split by two lone carriage returns each get their own note."""
    assert replace_unsendable_images("![x](bad)\r\r![x](bad)") == f"{_NOTE.format('x')}\n\n{_NOTE.format('x')}"


def test_references_links_and_math() -> None:
    """A reference image is replaced and its definition kept; an image inside a link is replaced; math is kept."""
    assert replace_unsendable_images("![x][r]\n\n[r]: ./a.png") == f"{_NOTE.format('x')}\n\n[r]: ./a.png"
    assert replace_unsendable_images("[![x](att)](https://a.example)") == f"[{_NOTE.format('x')}](https://a.example)"
    assert replace_unsendable_images("Math $![x](a)$ stays.") == "Math $![x](a)$ stays."
    assert replace_unsendable_images("![](att)") == _NOTE.format("image")
