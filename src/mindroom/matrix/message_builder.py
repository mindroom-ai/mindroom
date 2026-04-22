"""Matrix message content builder with proper threading support."""

import re
from collections.abc import Callable
from html import escape, unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pygments import highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

# Standard Matrix-safe HTML tags.
_GENERAL_FORMATTED_BODY_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "br",
        "caption",
        "code",
        "del",
        "details",
        "div",
        "em",
        "font",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "img",
        "li",
        "ol",
        "p",
        "pre",
        "s",
        "span",
        "strike",
        "strong",
        "sub",
        "summary",
        "sup",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    },
)

_ALLOWED_FORMATTED_BODY_TAGS = _GENERAL_FORMATTED_BODY_TAGS
_BLOCK_FORMATTED_BODY_TAGS = frozenset(
    {
        "blockquote",
        "caption",
        "details",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    },
)
assert _BLOCK_FORMATTED_BODY_TAGS <= _ALLOWED_FORMATTED_BODY_TAGS
_ALLOWED_FORMATTED_BODY_ATTRIBUTES = {
    "a": frozenset({"href", "title"}),
    "code": frozenset({"class"}),
    "details": frozenset({"open"}),
    "font": frozenset({"color", "data-mx-bg-color", "data-mx-color"}),
    "img": frozenset({"alt", "height", "src", "title", "width"}),
    "ol": frozenset({"start"}),
    "pre": frozenset({"class"}),
    "span": frozenset({"data-mx-bg-color", "data-mx-color", "data-mx-maths", "data-mx-spoiler", "style"}),
}
_VOID_FORMATTED_BODY_TAGS = frozenset({"br", "hr", "img"})
_URL_ATTRIBUTES = frozenset({"href", "src"})
_ALLOWED_URL_SCHEMES = frozenset({"", "http", "https", "mailto", "matrix", "mxc"})
_ALLOWED_STYLE_PROPERTIES = frozenset(
    {
        "background",
        "background-color",
        "border",
        "border-bottom",
        "border-left",
        "border-right",
        "border-top",
        "color",
        "font-style",
        "font-weight",
        "text-decoration",
    },
)
_SAFE_STYLE_VALUE_PATTERN = re.compile(r"[#(),.%\s0-9A-Za-z-]+")
_UNTERMINATED_HTML_FRAGMENT_PATTERN = re.compile(
    r"<(?:(?:!--)|(?:\?)|(?:![A-Za-z])|(?:/?[A-Za-z]))[^>\r\n]*(?=$|[\r\n])",
)
_RAW_HTML_TAG_LINE_START_PATTERN = re.compile(
    r"^([ ]{0,3})(</?([A-Za-z][A-Za-z0-9-]*)(?:\s+[^<>]*)?\s*/?>)",
)
_SUPPORTED_BLOCK_LINE_START_PATTERN = re.compile(
    rf"^[ ]{{0,3}}</?(?:{'|'.join(sorted(_BLOCK_FORMATTED_BODY_TAGS))})\b",
    re.IGNORECASE,
)
_SUPPORTED_BLOCK_END_TAG_PATTERN = re.compile(
    rf"</(?:{'|'.join(sorted(_BLOCK_FORMATTED_BODY_TAGS))})>\s*$",
    re.IGNORECASE,
)
_HR_BLOCK_TAG_PATTERN = re.compile(r"^[ ]{0,3}<hr\b[^<>]*/?>\s*$", re.IGNORECASE)
_FENCE_OPEN_PATTERN = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")


def _count_repeated_characters(text: str, start_index: int, character: str) -> int:
    end_index = start_index
    while end_index < len(text) and text[end_index] == character:
        end_index += 1
    return end_index - start_index


def _is_fence_closing_line(line: str, fence_character: str, opening_length: int) -> bool:
    stripped_line = line.lstrip(" ")
    if len(line) - len(stripped_line) > 3 or not stripped_line.startswith(fence_character):
        return False
    closing_length = _count_repeated_characters(stripped_line, 0, fence_character)
    if closing_length < opening_length:
        return False
    return stripped_line[closing_length:].strip() == ""


def _needs_block_html_boundary_after_line(line: str) -> bool:
    stripped_line = line.rstrip()
    if not stripped_line:
        return False
    if _HR_BLOCK_TAG_PATTERN.match(stripped_line):
        return True
    return (
        _SUPPORTED_BLOCK_LINE_START_PATTERN.match(stripped_line) is not None
        and _SUPPORTED_BLOCK_END_TAG_PATTERN.search(stripped_line) is not None
    )


def _transform_markdown_outside_fenced_code(text: str, transform: Callable[[str], str]) -> str:
    """Apply a transform only to Markdown outside fenced code blocks."""
    transformed_parts: list[str] = []
    prose_buffer: list[str] = []
    fence_buffer: list[str] = []
    fence_character: str | None = None
    opening_length = 0
    for line in text.splitlines(keepends=True):
        if fence_character is None:
            fence_match = _FENCE_OPEN_PATTERN.match(line)
            if fence_match is None:
                prose_buffer.append(line)
                continue
            if prose_buffer and _needs_block_html_boundary_after_line(prose_buffer[-1]):
                if prose_buffer[-1].endswith("\n"):
                    prose_buffer.append("\n")
                else:
                    prose_buffer[-1] = f"{prose_buffer[-1]}\n\n"
            transformed_parts.append(transform("".join(prose_buffer)))
            prose_buffer = []
            fence_character = fence_match.group(1)[0]
            opening_length = len(fence_match.group(1))
            fence_buffer.append(line)
            continue

        fence_buffer.append(line)
        if _is_fence_closing_line(line, fence_character, opening_length):
            transformed_parts.append("".join(fence_buffer))
            fence_buffer = []
            fence_character = None
            opening_length = 0

    if fence_buffer:
        transformed_parts.append("".join(prose_buffer))
        transformed_parts.append("".join(fence_buffer))
        return "".join(transformed_parts)

    transformed_parts.append(transform("".join(prose_buffer)))
    return "".join(transformed_parts)


def _escape_html_block_openers_in_text(text: str) -> str:
    """Escape comment/declaration/PI openers that CommonMark treats as raw HTML blocks."""
    escaped_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped_line = line.lstrip(" ")
        if len(line) - len(stripped_line) <= 3:
            if stripped_line.startswith(("<!--", "<!", "<?")):
                escaped_lines.append(line[: len(line) - len(stripped_line)] + escape(stripped_line))
                continue
            raw_tag_match = _RAW_HTML_TAG_LINE_START_PATTERN.match(line)
            if raw_tag_match is not None and raw_tag_match.group(3).lower() not in _ALLOWED_FORMATTED_BODY_TAGS:
                escaped_lines.append(raw_tag_match.group(1) + escape(line[len(raw_tag_match.group(1)) :]))
                continue
        escaped_lines.append(line)
    return "".join(escaped_lines)


def _escape_html_block_openers(text: str) -> str:
    return _transform_markdown_outside_fenced_code(text, _escape_html_block_openers_in_text)


def _sanitize_url_attribute(value: str) -> str | None:
    # TODO(ISSUE-084): split URL policy by tag and restrict img[src] to mxc:// only after the
    # formatted_body callers are audited for compatibility with existing content.
    candidate = unescape(value).strip()
    if not candidate:
        return None
    normalized = "".join(char for char in candidate if not char.isspace())
    if normalized.startswith(("#", "/", "//")):
        return candidate
    scheme = urlsplit(normalized).scheme.lower()
    if scheme in _ALLOWED_URL_SCHEMES:
        return candidate
    if not scheme and ":" not in normalized:
        return candidate
    return None


def _sanitize_style_attribute(value: str) -> str | None:
    sanitized_declarations: list[str] = []
    for declaration in value.split(";"):
        stripped_declaration = declaration.strip()
        if not stripped_declaration or ":" not in stripped_declaration:
            continue
        name, raw_style_value = stripped_declaration.split(":", 1)
        style_name = name.strip().lower()
        style_value = " ".join(raw_style_value.strip().split())
        if style_name not in _ALLOWED_STYLE_PROPERTIES or not style_value:
            continue
        lowered_style_value = style_value.lower()
        if "expression" in lowered_style_value or "url(" in lowered_style_value:
            continue
        if not _SAFE_STYLE_VALUE_PATTERN.fullmatch(style_value):
            continue
        sanitized_declarations.append(f"{style_name}: {style_value}")
    return "; ".join(sanitized_declarations) or None


def _normalize_input_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_supported_block_html_boundaries_in_text(text: str) -> str:
    """Insert the blank lines CommonMark needs after supported block HTML."""
    lines = text.splitlines(keepends=True)
    normalized_parts: list[str] = []
    for index, line in enumerate(lines):
        normalized_parts.append(line)
        if index == len(lines) - 1:
            continue
        stripped_line = line.rstrip()
        if not stripped_line:
            continue
        next_line = lines[index + 1]
        if not next_line.strip():
            continue
        # Only <hr> needs this special-case void-tag boundary fix because it is the
        # only allowed void tag that starts a CommonMark block HTML construct.
        if _needs_block_html_boundary_after_line(line):
            normalized_parts.append("\n")
    return "".join(normalized_parts)


def _normalize_supported_block_html_boundaries(text: str) -> str:
    return _transform_markdown_outside_fenced_code(text, _normalize_supported_block_html_boundaries_in_text)


def _escape_unterminated_html_fragments(html_text: str) -> str:
    """Escape malformed tag-like fragments so HTMLParser preserves them as text."""
    return _UNTERMINATED_HTML_FRAGMENT_PATTERN.sub(lambda match: escape(match.group(0)), html_text)


def _format_sanitized_attributes(tag_name: str, attrs: list[tuple[str, str | None]]) -> str:
    allowed_attributes = _ALLOWED_FORMATTED_BODY_ATTRIBUTES.get(tag_name, frozenset())
    sanitized_attributes: list[str] = []
    for attr_name, attr_value in attrs:
        normalized_attr_name = attr_name.lower()
        if normalized_attr_name not in allowed_attributes:
            continue
        if normalized_attr_name == "open":
            sanitized_attributes.append(" open")
            continue
        if attr_value is None:
            continue
        sanitized_value = attr_value
        if normalized_attr_name in _URL_ATTRIBUTES:
            sanitized_url = _sanitize_url_attribute(attr_value)
            if sanitized_url is None:
                continue
            sanitized_value = sanitized_url
        elif normalized_attr_name == "style":
            sanitized_style = _sanitize_style_attribute(attr_value)
            if sanitized_style is None:
                continue
            sanitized_value = sanitized_style
        sanitized_attributes.append(f' {normalized_attr_name}="{escape(sanitized_value, quote=True)}"')
    return "".join(sanitized_attributes)


class _FormattedBodyHtmlSanitizer(HTMLParser):
    """Strip unsafe attributes and escape unsupported tags in rendered HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._parts: list[str] = []

    def get_html(self) -> str:
        return "".join(self._parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        starttag_text = self.get_starttag_text() or ""
        if normalized_tag not in _ALLOWED_FORMATTED_BODY_TAGS:
            self._parts.append(escape(starttag_text))
            return
        self._parts.append(f"<{normalized_tag}{_format_sanitized_attributes(normalized_tag, attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        starttag_text = self.get_starttag_text() or ""
        if normalized_tag not in _ALLOWED_FORMATTED_BODY_TAGS:
            self._parts.append(escape(starttag_text))
            return
        if normalized_tag in _VOID_FORMATTED_BODY_TAGS:
            self._parts.append(f"<{normalized_tag}{_format_sanitized_attributes(normalized_tag, attrs)}>")
            return
        self._parts.append(
            f"<{normalized_tag}{_format_sanitized_attributes(normalized_tag, attrs)}></{normalized_tag}>",
        )

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag not in _ALLOWED_FORMATTED_BODY_TAGS:
            self._parts.append(escape(f"</{normalized_tag}>"))
            return
        self._parts.append(f"</{normalized_tag}>")

    def handle_data(self, data: str) -> None:
        self._parts.append(escape(data))

    def handle_entityref(self, name: str) -> None:
        self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self._parts.append(escape(f"<!--{data}-->"))

    def handle_decl(self, decl: str) -> None:
        self._parts.append(escape(f"<!{decl}>"))

    def handle_pi(self, data: str) -> None:
        self._parts.append(escape(f"<?{data}>"))

    def unknown_decl(self, data: str) -> None:
        # HTMLParser strips the trailing "]]>" from CDATA payloads passed here.
        self._parts.append(escape(f"<![{data}]]>"))


def _sanitize_formatted_body_html(html_text: str) -> str:
    sanitizer = _FormattedBodyHtmlSanitizer()
    sanitizer.feed(_escape_unterminated_html_fragments(html_text))
    sanitizer.close()
    return sanitizer.get_html()


_HIGHLIGHT_FORMATTER = HtmlFormatter(noclasses=True, nowrap=True)


def _highlight(code: str, lang: str, _attrs: str) -> str:
    """Pygments syntax-highlight callback for markdown-it-py."""
    if not lang:
        return ""
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        return ""
    return highlight(code, lexer, _HIGHLIGHT_FORMATTER)


def _render_preserved_math_inline(
    _self: object,
    tokens: list[Token],
    idx: int,
    _options: object,
    _env: object,
) -> str:
    return escape(f"${tokens[idx].content}$")


def _render_preserved_math_block(
    _self: object,
    tokens: list[Token],
    idx: int,
    _options: object,
    _env: object,
) -> str:
    content = f"$$\n{tokens[idx].content.strip()}\n$$"
    return f"<div>{escape(content)}</div>\n"


def _build_markdown_renderer() -> MarkdownIt:
    renderer = MarkdownIt("commonmark", {"breaks": True, "highlight": _highlight})
    renderer.enable("table")
    renderer.enable("strikethrough")
    renderer.use(
        dollarmath_plugin,
        allow_labels=False,
        allow_space=False,
        allow_digits=False,
    )
    renderer.add_render_rule("math_inline", _render_preserved_math_inline)
    renderer.add_render_rule("math_block", _render_preserved_math_block)
    return renderer


_MARKDOWN_RENDERER = _build_markdown_renderer()


def markdown_to_html(text: str) -> str:
    """Convert markdown text to HTML for Matrix formatted messages.

    Uses markdown-it-py with ``breaks=True`` (replaces ``nl2br`` — newlines
    become ``<br>`` inside paragraphs) and GFM table + strikethrough rules.
    Unlike the old ``markdown`` library, tables parse correctly even without a
    blank line before them.
    """
    normalized_input = _normalize_input_line_endings(text)
    escaped_text = _escape_html_block_openers(normalized_input)
    normalized_text = _normalize_supported_block_html_boundaries(escaped_text)
    html_text: str = _MARKDOWN_RENDERER.render(normalized_text)
    return _sanitize_formatted_body_html(html_text)


def _build_thread_relation(
    thread_event_id: str,
    reply_to_event_id: str | None = None,
    latest_thread_event_id: str | None = None,
) -> dict[str, Any]:
    """Build the m.relates_to structure for thread messages per MSC3440.

    Args:
        thread_event_id: The thread root event ID
        reply_to_event_id: Optional event ID for genuine replies within thread
        latest_thread_event_id: Latest event in thread (required for fallback if no reply_to)

    Returns:
        The m.relates_to structure for the message content

    """
    if reply_to_event_id:
        # Genuine reply to a specific message in the thread
        return {
            "rel_type": "m.thread",
            "event_id": thread_event_id,
            "is_falling_back": False,
            "m.in_reply_to": {"event_id": reply_to_event_id},
        }
    # Fallback: continuing thread without specific reply
    # Per MSC3440, should point to latest message in thread for backwards compatibility
    assert latest_thread_event_id is not None, "latest_thread_event_id is required for thread fallback"
    return {
        "rel_type": "m.thread",
        "event_id": thread_event_id,
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": latest_thread_event_id},
    }


def build_message_content(
    body: str,
    formatted_body: str | None = None,
    mentioned_user_ids: list[str] | None = None,
    thread_event_id: str | None = None,
    reply_to_event_id: str | None = None,
    latest_thread_event_id: str | None = None,
    extra_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete Matrix message content dictionary.

    This handles all the Matrix protocol requirements for messages including:
    - Basic message structure
    - HTML formatting
    - User mentions
    - Thread relations (MSC3440 compliant)
    - Reply relations

    Args:
        body: The plain text message body
        formatted_body: Optional HTML formatted body (if not provided, converts from markdown)
        mentioned_user_ids: Optional list of Matrix user IDs to mention
        thread_event_id: Optional thread root event ID
        reply_to_event_id: Optional event ID to reply to
        latest_thread_event_id: Optional latest event in thread (for MSC3440 fallback)
        extra_content: Optional extra content fields to merge into the message

    Returns:
        Complete content dictionary ready for room_send

    """
    content: dict[str, Any] = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_body or markdown_to_html(body),
    }

    # Add mentions if any
    if mentioned_user_ids:
        content["m.mentions"] = {"user_ids": mentioned_user_ids}

    # Add thread/reply relationship if specified
    if thread_event_id:
        content["m.relates_to"] = _build_thread_relation(
            thread_event_id=thread_event_id,
            reply_to_event_id=reply_to_event_id,
            latest_thread_event_id=latest_thread_event_id,
        )
    elif reply_to_event_id:
        # Plain reply without thread (shouldn't happen in this bot)
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to_event_id}}

    if extra_content:
        content.update(extra_content)

    return content
