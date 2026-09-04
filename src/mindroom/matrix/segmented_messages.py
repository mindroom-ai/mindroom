"""Split oversized text responses into complete Matrix message events.

Matrix has a hard per-event size limit.  A long response should therefore be
represented by several ordinary rich-text events instead of one truncated
preview plus a private sidecar.  The first event can still be an edit of the
streaming placeholder; subsequent events are thread replies.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.matrix.large_messages import calculate_event_size, is_edit_message
from mindroom.matrix.message_builder import markdown_to_html

if TYPE_CHECKING:
    from collections.abc import Callable

# Budgets stay below the ``large_messages`` sidecar thresholds so every segment
# is sent as built. Encrypted budgets leave room for Megolm base64 expansion.
_SEGMENT_TARGET_PLAINTEXT_BYTES = 46_000
_SEGMENT_TARGET_EDIT_BYTES = 22_000
_SEGMENT_TARGET_ENCRYPTED_PLAINTEXT_BYTES = 30_000
_SEGMENT_TARGET_ENCRYPTED_EDIT_BYTES = 16_000
_SEGMENT_SHRINK_MARGIN = 0.9
# Useful for rebuilding model context, but not part of the visible answer. A
# long tool trace would otherwise push a segmentable response into a sidecar.
_INTERNAL_STREAM_KEYS = frozenset({"io.mindroom.tool_trace", "io.mindroom.visible_body"})
# Rebuilt per segment from the chunk it carries, or dropped as internal.
_SEGMENT_DROPPED_KEYS = _INTERNAL_STREAM_KEYS | {
    "body",
    "format",
    "formatted_body",
    "m.new_content",
    "m.relates_to",
    "m.mentions",
}
_MENTION_PILL_PATTERN = re.compile(r'<a href="(?P<href>https://matrix\.to/#/[^"]+)">[^<]+</a>')
_CODE_REGION_PATTERN = re.compile(r"(<code\b[^>]*>.*?</code>)", re.DOTALL)
_FENCE_LINE_PATTERN = re.compile(r"[ \t]*((?:>[ \t]{0,3})*)(`{3,}|~{3,})(.*)")


def _user_id_boundary_pattern(user_ids: list[str]) -> re.Pattern[str]:
    """Match whole user IDs only, longest first, never inside a longer token.

    A bare substring test both attributes a prefix-overlapping ID (``@a:s`` inside
    ``@a:s2``) and matches inside URLs; the lookarounds restrict matches to
    positions the mention pipeline could have produced.
    """
    alternation = "|".join(re.escape(user_id) for user_id in sorted(user_ids, key=lambda uid: -len(uid)))
    return re.compile(r"(?<![\w@.:/=-])(?:" + alternation + r")(?![\w:/=-])")


@dataclass(frozen=True, slots=True)
class SegmentedMatrixContent:
    """The first event payload and the continuation event payloads."""

    first: dict[str, Any]
    continuations: tuple[dict[str, Any], ...]


def _source_content(content: dict[str, Any]) -> dict[str, Any] | None:
    if is_edit_message(content):
        replacement = content.get("m.new_content")
        return replacement if isinstance(replacement, dict) else None
    return content


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


def _chunk_mentions(source: Mapping[str, Any], body: str) -> dict[str, Any] | None:
    """Return the mention metadata for just the mentions one chunk carries.

    ``m.mentions`` must travel with the event whose body carries the mention;
    leaving every mentioned user on the first segment both misattributes the
    mention and can notify a user whose name never visibly arrived. A room
    mention follows the same rule for the ``@room`` text.
    """
    mentions = source.get("m.mentions")
    if not isinstance(mentions, Mapping):
        return None
    chunk_mentions: dict[str, Any] = {}
    user_ids = mentions.get("user_ids")
    if isinstance(user_ids, list):
        candidates = [user_id for user_id in user_ids if isinstance(user_id, str)]
        if candidates:
            present = set(_user_id_boundary_pattern(candidates).findall(body))
            if present:
                chunk_mentions["user_ids"] = [user_id for user_id in candidates if user_id in present]
    if mentions.get("room") is True and "@room" in body:
        chunk_mentions["room"] = True
    return chunk_mentions or None


def _render_segment_html(source: Mapping[str, Any], body: str) -> str:
    """Render one chunk, restoring mention pills its plain body cannot carry.

    Substitution is boundary-aware and skips code regions: a user ID appearing
    as literal text (code span, URL path) is content, not a mention.
    """
    formatted = markdown_to_html(body)
    pills = _mention_pills(source)
    if not pills:
        return formatted
    pattern = _user_id_boundary_pattern(list(pills))
    parts = _CODE_REGION_PATTERN.split(formatted)
    return "".join(
        part if part.startswith("<code") else pattern.sub(lambda match: pills[match.group(0)], part) for part in parts
    )


def _rich_text_content(
    source: Mapping[str, Any],
    body: str,
    *,
    relation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one normal rich-text content payload from one body chunk."""
    content = {key: value for key, value in source.items() if key not in _SEGMENT_DROPPED_KEYS}
    content.setdefault("msgtype", "m.text")
    content["body"] = body
    content["format"] = "org.matrix.custom.html"
    content["formatted_body"] = _render_segment_html(source, body)
    mentions = _chunk_mentions(source, body)
    if mentions is not None:
        content["m.mentions"] = mentions
    if relation is not None:
        content["m.relates_to"] = dict(relation)
    return content


def _edit_content_chunk(content: dict[str, Any], source: dict[str, Any], body: str) -> dict[str, Any]:
    """Build the first edit event with one complete rich-text chunk.

    The outer envelope keeps the source edit's own ``m.mentions`` -- for an
    edit that is the revision delta, and re-deriving it from one chunk could
    re-notify recipients an earlier revision already mentioned. The resolved
    per-chunk set lives under ``m.new_content``.
    """
    replacement = _rich_text_content(source, body)
    edit = {key: value for key, value in content.items() if key not in _INTERNAL_STREAM_KEYS}
    edit["body"] = f"* {body}"
    edit["format"] = "org.matrix.custom.html"
    edit["formatted_body"] = replacement["formatted_body"]
    edit["m.new_content"] = replacement
    return edit


def _continuation_content(
    source: dict[str, Any],
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
    relation = (
        {
            "rel_type": "m.thread",
            "event_id": thread_id,
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": reply_to_event_id or thread_id},
        }
        if thread_id
        else None
    )
    return _rich_text_content(source, body, relation=relation)


def _segment_target_bytes(*, room_encrypted: bool, is_edit: bool) -> int:
    """Return a conservative per-event budget for one response segment."""
    budgets = {
        (False, False): _SEGMENT_TARGET_PLAINTEXT_BYTES,
        (False, True): _SEGMENT_TARGET_EDIT_BYTES,
        (True, False): _SEGMENT_TARGET_ENCRYPTED_PLAINTEXT_BYTES,
        (True, True): _SEGMENT_TARGET_ENCRYPTED_EDIT_BYTES,
    }
    return budgets[room_encrypted, is_edit]


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
    in the other. Chunks always begin fence-balanced, so a single scan finds
    the offending opener. Per CommonMark a fence closes only on the same
    marker character, at least the opening length, and nothing but trailing
    whitespace, and a fence opened inside a block quote closes only at the
    same quote depth.
    """
    opener: tuple[int, str, int, int] | None = None
    pos = start
    while pos < end:
        line_end = text.find("\n", pos, end)
        line_end = end if line_end == -1 else line_end
        match = _FENCE_LINE_PATTERN.match(text[pos:line_end])
        if match:
            quote_depth = match.group(1).count(">")
            marker = match.group(2)
            if opener is None:
                opener = (quote_depth, marker[0], len(marker), pos)
            elif (
                quote_depth == opener[0]
                and marker[0] == opener[1]
                and len(marker) >= opener[2]
                and not match.group(3).strip()
            ):
                opener = None
        pos = line_end + 1
    return opener[3] if opener is not None else None


def _fitting_end(text: str, start: int, build: Callable[[str], dict[str, Any]], target_bytes: int) -> int | None:
    """Return the largest end whose built event fits, or None when one character does not.

    Rendering Markdown is the expensive step, so instead of bisecting the
    search shrinks the chunk in proportion to the measured overshoot; a chunk
    normally converges in one to three renders.
    """
    overhead = calculate_event_size(build(""))
    budget = target_bytes - overhead
    if budget <= 0:
        return None
    end = min(len(text), start + budget)
    while True:
        used = calculate_event_size(build(text[start:end])) - overhead
        if used <= budget:
            return end
        if end == start + 1:
            return None
        end = start + max(1, int((end - start) * budget / used * _SEGMENT_SHRINK_MARGIN))


def _split_body(
    text: str,
    first_build: Callable[[str], dict[str, Any]],
    continuation_build: Callable[[str], dict[str, Any]],
    *,
    first_target: int,
    continuation_target: int,
) -> list[str] | None:
    """Split text at fitting Markdown boundaries while retaining every character."""
    chunks: list[str] = []
    offset = 0
    while offset < len(text):
        build, target = (first_build, first_target) if offset == 0 else (continuation_build, continuation_target)
        end = _fitting_end(text, offset, build, target)
        if end is None:
            return None
        if end < len(text):
            end = _preferred_boundary(text, offset, end)
        fence_start = _unclosed_fence_start(text, offset, end)
        if fence_start is not None:
            if fence_start == offset:
                # One fence spans the whole remaining budget; no cut can keep
                # every segment self-contained, so the sidecar keeps it intact.
                return None
            end = fence_start
        chunks.append(text[offset:end])
        offset = end
    return chunks or None


def segment_matrix_content(
    content: dict[str, Any],
    *,
    room_encrypted: bool,
    continuation_thread_id: str | None = None,
    continuation_reply_to_event_id: str | None = None,
) -> SegmentedMatrixContent | None:
    """Return lossless rich-text payloads when one event is too large.

    The first segment is sized against the edit budget when the source is an
    edit; continuations are plain messages and use the larger plain budget.
    Internal streaming metadata that is not needed by the visible answer is
    omitted before sizing, so metadata alone cannot force a Markdown response
    into a sidecar. A ``None`` result means the payload is not a plain text
    response, its fixed metadata alone is too large, or a code fence cannot be
    kept whole; the caller uses the sidecar fallback in those cases.
    """
    source = _source_content(content)
    if source is None or not isinstance(source.get("body"), str):
        return None

    is_edit = is_edit_message(content)
    first_target = _segment_target_bytes(room_encrypted=room_encrypted, is_edit=is_edit)
    if calculate_event_size(content) <= first_target:
        return None
    continuation_target = _segment_target_bytes(room_encrypted=room_encrypted, is_edit=False)

    def first_build(chunk: str) -> dict[str, Any]:
        if is_edit:
            return _edit_content_chunk(content, source, chunk)
        return _rich_text_content(source, chunk, relation=source.get("m.relates_to"))

    def continuation_build(chunk: str) -> dict[str, Any]:
        return _continuation_content(
            source,
            chunk,
            thread_id=continuation_thread_id,
            reply_to_event_id=continuation_reply_to_event_id,
        )

    chunks = _split_body(
        source["body"],
        first_build,
        continuation_build,
        first_target=first_target,
        continuation_target=continuation_target,
    )
    if chunks is None:
        return None
    first = first_build(chunks[0])
    continuations = tuple(continuation_build(chunk) for chunk in chunks[1:])
    if calculate_event_size(first) > first_target or any(
        calculate_event_size(candidate) > continuation_target for candidate in continuations
    ):
        return None
    return SegmentedMatrixContent(first=first, continuations=continuations)


__all__ = ["SegmentedMatrixContent", "segment_matrix_content"]
