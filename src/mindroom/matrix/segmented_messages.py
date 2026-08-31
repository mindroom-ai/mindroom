"""Split oversized text responses into complete Matrix message events.

Matrix has a hard per-event size limit.  A long response should therefore be
represented by several ordinary rich-text events instead of one truncated
preview plus a private sidecar.  The first event can still be an edit of the
streaming placeholder; subsequent events are thread replies.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mindroom.matrix.message_builder import markdown_to_html

_SEGMENT_TARGET_PLAINTEXT_BYTES = 46_000
_SEGMENT_TARGET_EDIT_BYTES = 22_000
_SEGMENT_TARGET_ENCRYPTED_PLAINTEXT_BYTES = 30_000
_SEGMENT_TARGET_ENCRYPTED_EDIT_BYTES = 16_000
_SEGMENT_FIRST_DROPPED_KEYS = frozenset(
    {
        # These fields are useful for rebuilding model context, but are not
        # part of the visible answer. A long tool trace can otherwise make an
        # otherwise segmentable response fall back to large_messages.py.
        "io.mindroom.tool_trace",
        "io.mindroom.visible_body",
    },
)
_SEGMENT_METADATA_KEYS = frozenset(
    {
        "io.mindroom.tool_trace",
        "io.mindroom.visible_body",
        "m.mentions",
    },
)
_MENTION_PILL_PATTERN = re.compile(r'<a href="(?P<href>https://matrix\.to/#/[^"]+)">[^<]+</a>')
_FENCE_MARKER_PATTERN = re.compile(r"^[ \t]*(`{3,}|~{3,})", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class SegmentedMatrixContent:
    """The first event payload and the continuation event payloads."""

    first: dict[str, Any]
    continuations: tuple[dict[str, Any], ...]


def _estimated_event_size(content: Mapping[str, Any]) -> int:
    """Estimate Matrix event bytes with the same conservative overhead as MindRoom."""
    canonical = json.dumps(dict(content), sort_keys=True, separators=(",", ":"))
    return len(canonical.encode("utf-8")) + 2000


def _is_edit(content: Mapping[str, Any]) -> bool:
    relation = content.get("m.relates_to")
    return "m.new_content" in content or (isinstance(relation, Mapping) and relation.get("rel_type") == "m.replace")


def _source_content(content: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if _is_edit(content):
        replacement = content.get("m.new_content")
        return replacement if isinstance(replacement, Mapping) else None
    return content


def _continuation_metadata(source: Mapping[str, Any]) -> dict[str, Any]:
    """Copy small semantic metadata without repeating bulky stream payloads."""
    return {
        key: value
        for key, value in source.items()
        if key not in {"body", "format", "formatted_body", "m.new_content", "m.relates_to"}
        and key not in _SEGMENT_METADATA_KEYS
    }


def _first_segment_metadata(source: Mapping[str, Any]) -> dict[str, Any]:
    """Copy visible-event metadata without internal streaming baggage."""
    return {
        key: value
        for key, value in source.items()
        if key not in {"body", "format", "formatted_body", "m.new_content", "m.relates_to"}
        and key not in _SEGMENT_FIRST_DROPPED_KEYS
    }


def _mention_pills(source: Mapping[str, Any]) -> dict[str, str]:
    """Map each mentioned user ID to its pill anchor from the rendered source.

    The plain ``body`` carries only the bare user ID where a mention goes; the
    pill link exists solely in ``formatted_body``. Re-rendering one chunk from
    its body would silently drop the link, so the anchor is copied across.
    """
    formatted = source.get("formatted_body")
    if not isinstance(formatted, str):
        return {}
    pills: dict[str, str] = {}
    for match in _MENTION_PILL_PATTERN.finditer(formatted):
        target = match.group("href").removeprefix("https://matrix.to/#/")
        if target.startswith("@"):
            pills[target] = match.group(0)
    return pills


def _render_segment_html(source: Mapping[str, Any], body: str) -> str:
    """Render one chunk, restoring mention pills its plain body cannot carry."""
    formatted = markdown_to_html(body)
    for user_id, pill in sorted(_mention_pills(source).items(), key=lambda item: -len(item[0])):
        formatted = formatted.replace(user_id, pill)
    return formatted


def _rich_text_content(
    source: Mapping[str, Any],
    body: str,
    *,
    include_mentions: bool,
    include_relation: bool = False,
) -> dict[str, Any]:
    """Build one normal rich-text content payload from one body chunk."""
    if include_mentions:
        # Keep mentions and other user-visible delivery metadata, but do not
        # let internal tool-trace data consume the event budget needed for
        # Markdown rendering.
        candidate = _first_segment_metadata(source)
    else:
        candidate = _continuation_metadata(source)
        candidate["msgtype"] = source.get("msgtype", "m.text")
    candidate["body"] = body
    candidate["format"] = "org.matrix.custom.html"
    candidate["formatted_body"] = _render_segment_html(source, body)
    if include_relation:
        relation = source.get("m.relates_to")
        if isinstance(relation, Mapping):
            candidate["m.relates_to"] = dict(relation)
    return candidate


def _edit_content_chunk(
    content: Mapping[str, Any],
    source: Mapping[str, Any],
    body: str,
) -> dict[str, Any]:
    """Build the first edit event with one complete rich-text chunk."""
    replacement = _rich_text_content(source, body, include_mentions=True)
    candidate = {key: value for key, value in content.items() if key not in _SEGMENT_FIRST_DROPPED_KEYS}
    candidate["body"] = f"* {body}"
    candidate["format"] = "org.matrix.custom.html"
    candidate["formatted_body"] = _render_segment_html(source, candidate["body"])
    candidate["m.new_content"] = replacement
    return candidate


def _thread_relation(thread_id: str | None, reply_to_event_id: str | None) -> dict[str, Any] | None:
    if not thread_id:
        return None
    return {
        "rel_type": "m.thread",
        "event_id": thread_id,
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": reply_to_event_id or thread_id},
    }


def _continuation_content(
    source: Mapping[str, Any],
    body: str,
    *,
    thread_id: str | None,
    reply_to_event_id: str | None,
) -> dict[str, Any]:
    """Build a continuation that recovery cannot mistake for a fresh answer.

    Copying the source relation verbatim would repeat a genuine reply to the
    turn's source event, and replay recovery counts every such event as a
    visible response of the turn -- several for one segmented answer, which it
    refuses. Continuations therefore always take the thread-fallback form (or
    no relation outside threads), which recovery explicitly excludes.
    """
    candidate = _rich_text_content(source, body, include_mentions=False)
    relation = _thread_relation(thread_id, reply_to_event_id)
    if relation is not None:
        candidate["m.relates_to"] = relation
    return candidate


def _segment_target_bytes(*, room_encrypted: bool, is_edit: bool) -> int:
    """Return a conservative per-event budget for one response segment."""
    budgets = {
        (False, False): _SEGMENT_TARGET_PLAINTEXT_BYTES,
        (False, True): _SEGMENT_TARGET_EDIT_BYTES,
        (True, False): _SEGMENT_TARGET_ENCRYPTED_PLAINTEXT_BYTES,
        (True, True): _SEGMENT_TARGET_ENCRYPTED_EDIT_BYTES,
    }
    return budgets[room_encrypted, is_edit]


def _segment_builders(
    content: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    is_edit: bool,
    continuation_thread_id: str | None,
    continuation_reply_to_event_id: str | None,
) -> tuple[
    Callable[[str], dict[str, Any]],
    Callable[[str], dict[str, Any]],
    Callable[[str], int],
]:
    """Build first, continuation, and worst-case sizing functions."""
    if is_edit:

        def first_builder(chunk: str) -> dict[str, Any]:
            return _edit_content_chunk(content, source, chunk)

    else:

        def first_builder(chunk: str) -> dict[str, Any]:
            return _rich_text_content(
                source,
                chunk,
                include_mentions=True,
                include_relation=True,
            )

    def continuation_builder(chunk: str) -> dict[str, Any]:
        return _continuation_content(
            source,
            chunk,
            thread_id=continuation_thread_id,
            reply_to_event_id=continuation_reply_to_event_id,
        )

    def size_builder(chunk: str) -> int:
        return max(
            _estimated_event_size(first_builder(chunk)),
            _estimated_event_size(continuation_builder(chunk)),
        )

    return first_builder, continuation_builder, size_builder


def _preferred_boundary(text: str, start: int, end: int) -> int:
    """Prefer a paragraph or line boundary without making chunks too small."""
    minimum = start + max(1, (end - start) // 3)
    paragraph = text.rfind("\n\n", start + 1, end)
    if paragraph >= minimum and paragraph + 2 <= end:
        return paragraph + 2
    line = text.rfind("\n", start + 1, end)
    if line >= minimum and line + 1 <= end:
        return line + 1
    return end


def _unclosed_fence_start(text: str, start: int, end: int) -> int | None:
    """Return where an unclosed fenced code block opens inside text[start:end].

    Segments are rendered as standalone Markdown, so a cut inside a fence
    renders the remainder as plain text in one segment and a dangling opener
    in the other. Chunks always begin fence-balanced, so a single toggle scan
    finds the offending opener.
    """
    opener_start: int | None = None
    for match in _FENCE_MARKER_PATTERN.finditer(text, start, end):
        opener_start = None if opener_start is not None else match.start()
    return opener_start


def _split_body(
    text: str,
    builder: Callable[[str], dict[str, Any]],
    *,
    target_bytes: int,
    size_builder: Callable[[str], int] | None = None,
) -> list[str] | None:
    """Split text at fitting Unicode boundaries while retaining every character."""
    fits = size_builder or (lambda chunk: _estimated_event_size(builder(chunk)))
    if not text:
        return [text]
    if fits(text) <= target_bytes:
        return [text]

    chunks: list[str] = []
    offset = 0
    while offset < len(text):
        lower = offset + 1
        upper = len(text)
        best = offset
        while lower <= upper:
            candidate_end = (lower + upper) // 2
            if fits(text[offset:candidate_end]) <= target_bytes:
                best = candidate_end
                lower = candidate_end + 1
            else:
                upper = candidate_end - 1
        if best == offset:
            return None
        end = _preferred_boundary(text, offset, best)
        fence_start = _unclosed_fence_start(text, offset, end)
        if fence_start is not None:
            if fence_start == offset:
                # One fence spans the whole remaining budget; no cut can keep
                # every segment self-contained, so the sidecar keeps it intact.
                return None
            end = fence_start
        chunks.append(text[offset:end])
        offset = end
    return chunks


def segment_matrix_content(
    content: Mapping[str, Any],
    *,
    room_encrypted: bool,
    continuation_thread_id: str | None = None,
    continuation_reply_to_event_id: str | None = None,
) -> SegmentedMatrixContent | None:
    """Return lossless rich-text payloads when one event is too large.

    The thresholds intentionally stay below MindRoom's sidecar thresholds so
    every returned event passes through ``large_messages`` unchanged. Internal
    streaming metadata that is not needed by the visible answer is omitted
    from the first event before sizing, so metadata alone cannot force a
    Markdown response into a sidecar. A ``None`` result means the payload is
    not a plain text response or its remaining fixed metadata alone is too
    large; the caller can use the normal sidecar fallback in those cases.
    """
    source = _source_content(content)
    if source is None or not isinstance(source.get("body"), str):
        return None

    is_edit = _is_edit(content)
    target_bytes = _segment_target_bytes(room_encrypted=room_encrypted, is_edit=is_edit)
    if _estimated_event_size(content) <= target_bytes:
        return None

    body = source["body"]
    first_builder, continuation_builder, size_builder = _segment_builders(
        content,
        source,
        is_edit=is_edit,
        continuation_thread_id=continuation_thread_id,
        continuation_reply_to_event_id=continuation_reply_to_event_id,
    )

    chunks = _split_body(body, first_builder, target_bytes=target_bytes, size_builder=size_builder)
    if chunks is None:
        return None

    first = first_builder(chunks[0])
    continuations = tuple(continuation_builder(chunk) for chunk in chunks[1:])
    if any(_estimated_event_size(candidate) > target_bytes for candidate in (first, *continuations)):
        return None
    return SegmentedMatrixContent(first=first, continuations=continuations)


__all__ = ["SegmentedMatrixContent", "segment_matrix_content"]
