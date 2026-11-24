"""Focused contract tests for message_builder markdown boundary handling."""

from __future__ import annotations

import pytest

from mindroom.matrix.message_builder import (
    build_matrix_edit_content,
    build_message_content,
    build_reaction_content,
    build_thread_relation,
    markdown_to_html,
    opens_with_markdown_block,
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


def test_details_with_blank_lines_renders_inner_markdown() -> None:
    """Blank lines around Markdown inside ``<details>`` let CommonMark render it."""
    html = markdown_to_html("<details><summary>More</summary>\n\n**bold**\n\n</details>")
    assert html == "<details><summary>More</summary>\n<p><strong>bold</strong></p>\n</details>"


def test_matrix_spoiler_span_is_preserved_and_pipes_stay_literal() -> None:
    """Matrix spoiler spans keep both valueless and reasoned ``data-mx-spoiler``; ``||spoiler||`` is not converted."""
    assert markdown_to_html("<span data-mx-spoiler>**secret**</span>") == (
        "<p><span data-mx-spoiler><strong>secret</strong></span></p>\n"
    )
    assert markdown_to_html('<span data-mx-spoiler="plot">twist</span>') == (
        '<p><span data-mx-spoiler="plot">twist</span></p>\n'
    )
    assert markdown_to_html("||secret||") == "<p>||secret||</p>\n"


def test_matrix_event_autolink_does_not_pair_dollar_signs_as_math() -> None:
    """Autolinking a Matrix event URL prevents its ``$event_id`` from pairing with a later ``$`` as inline math."""
    html = markdown_to_html(
        "<https://matrix.to/#/!room:example.org/$first> — <span data-mx-spoiler>hidden</span>. "
        "[Discussion](https://matrix.to/#/!room:example.org/$next)",
    )
    assert html == (
        '<p><a href="https://matrix.to/#/!room:example.org/$first">'
        "https://matrix.to/#/!room:example.org/$first</a> — "
        "<span data-mx-spoiler>hidden</span>. "
        '<a href="https://matrix.to/#/!room:example.org/$next">Discussion</a></p>\n'
    )


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
        ("![a](mxc://[2001:db8::1]:8448/id)", '<p><img src="mxc://[2001:db8::1]:8448/id" alt="a"></p>\n'),
        ("![a](https://[2001:db8::1]/x.png)", '<p><img src="https://[2001:db8::1]/x.png" alt="a"></p>\n'),
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
        ("![x](//example.org/x.png)", '<p><img src="//example.org/x.png" alt="x"></p>\n'),
        ('<img src="//example.org:8443/x.png" alt="x">', '<img src="//example.org:8443/x.png" alt="x">'),
        ('<img src="//" alt="x">', "⚠️ Image not sent: x (no uploaded file)"),
        ('<img src="///x" alt="x">', "⚠️ Image not sent: x (no uploaded file)"),
        ('<img src="//a b/x" alt="x">', "⚠️ Image not sent: x (no uploaded file)"),
        ('<img src="//example.org:99999/x" alt="x">', "⚠️ Image not sent: x (no uploaded file)"),
    ],
)
def test_the_checked_src_is_the_emitted_src(markdown: str, expected: str) -> None:
    """The checked src is the emitted src: bracketed IPv6 is kept as written; whitespace in a host or an mxc id cannot load."""
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
        ("![<b>x</b>](bad)", "⚠️ Image not sent: &lt;b&gt;x&lt;/b&gt; (no uploaded file)"),
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


def test_numbered_and_repeated_footnotes_render_expected_html() -> None:
    """Numbered footnotes and repeated references emit sanitized Matrix HTML with backrefs."""
    html = markdown_to_html("Note[^1] and again[^1].\n\n[^1]: Detail.\n")
    assert html == (
        '<p>Note<sup class="footnote-ref"><a href="#fn1" id="fnref1">[1]</a></sup> '
        'and again<sup class="footnote-ref"><a href="#fn1" id="fnref1:1">[1:1]</a></sup>.</p>\n'
        '<hr class="footnotes-sep">\n'
        '<section class="footnotes">\n'
        '<ol class="footnotes-list">\n'
        '<li id="fn1" class="footnote-item"><p>Detail. '
        '<a href="#fnref1" class="footnote-backref">↩︎</a> '
        '<a href="#fnref1:1" class="footnote-backref">↩︎</a></p>\n'
        "</li>\n"
        "</ol>\n"
        "</section>\n"
    )


def test_named_and_multi_paragraph_footnotes_render() -> None:
    """Named labels and indented continuation paragraphs render inside the footnote item."""
    html = markdown_to_html("Claim[^src].\n\n[^src]: First paragraph.\n\n    Second paragraph.\n")
    assert html == (
        '<p>Claim<sup class="footnote-ref"><a href="#fn1" id="fnref1">[1]</a></sup>.</p>\n'
        '<hr class="footnotes-sep">\n'
        '<section class="footnotes">\n'
        '<ol class="footnotes-list">\n'
        '<li id="fn1" class="footnote-item"><p>First paragraph.</p>\n'
        '<p>Second paragraph. <a href="#fnref1" class="footnote-backref">↩︎</a></p>\n'
        "</li>\n"
        "</ol>\n"
        "</section>\n"
    )


def test_inline_footnote_syntax_and_undefined_refs_stay_literal() -> None:
    """Regexes like ``^[a-z]`` are not inline footnotes, and undefined ``[^1]`` stays literal while streaming."""
    assert markdown_to_html("Match ^[a-z]+ and pending[^1].") == "<p>Match ^[a-z]+ and pending[^1].</p>\n"
    assert (
        markdown_to_html("Inline `[^1]` stays code.\n\n[^1]: Unused.")
        == "<p>Inline <code>[^1]</code> stays code.</p>\n"
    )

    streamed = "Fact[^1].\n\n[^1]: Source."
    for end in range(1, len(streamed) + 1):
        prefix = streamed[:end]
        rendered = markdown_to_html(prefix)
        if "[^1]:" not in prefix and "[^1]" in prefix:
            assert "[^1]" in rendered
            assert 'class="footnote-ref"' not in rendered


@pytest.mark.parametrize(
    ("raw_html", "expected"),
    [
        ('<a id="evil" class="evil" href="#fn1">x</a>', '<p><a href="#fn1">x</a></p>\n'),
        ('<a id="fnref1" class="footnote-ref" href="javascript:alert(1)">x</a>', '<p><a id="fnref1">x</a></p>\n'),
        (
            '<a id="fn1&#10;" class="footnote-backref extra" href="#fnref1:1">x</a>',
            '<p><a href="#fnref1:1">x</a></p>\n',
        ),
        ('<a id="fn\u0661" href="#fn1">x</a>', '<p><a href="#fn1">x</a></p>\n'),
        ('<li id="bad" class="evil">item</li>', "<li>item</li>"),
        ('<li id="fn1:a" class="footnotes">item</li>', "<li>item</li>"),
        ('<section id="fn1" class="evil">body</section>', "<section>body</section>"),
        ('<sup id="fnref1" class="evil">1</sup>', "<p><sup>1</sup></p>\n"),
        ('<hr class="evil">', "<hr>"),
        ('<ol id="fn1" class="evil" start="2"><li>a</li></ol>', '<ol start="2"><li>a</li></ol>'),
        (
            '<pre class="language-python"><code class="language-python">x = 1</code></pre>',
            '<pre class="language-python"><code class="language-python">x = 1</code></pre>',
        ),
    ],
)
def test_footnote_sanitizer_strips_arbitrary_ids_and_classes(raw_html: str, expected: str) -> None:
    """Only exact footnote ids and tag-specific classes survive sanitization; code/pre classes stay intact."""
    assert markdown_to_html(raw_html) == expected


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("[^1]: Detail.\n\nSee[^1].", True),
        ("[^1]: Detail.\nSee[^1].", True),
        ("[^1]:\nSee[^1].", True),
        ("[^1] starts a paragraph.\n\n[^1]: Detail.", False),
        ("Note[^1]\n[^1]: Detail.", False),
    ],
)
def test_opens_with_markdown_block_handles_footnotes(markdown: str, expected: bool) -> None:
    """Footnote definitions open a line-start block; paragraphs opening with a reference do not."""
    assert opens_with_markdown_block(markdown) is expected
