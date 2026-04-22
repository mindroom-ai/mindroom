"""Trusted canonical visible-body helpers for Matrix message surfaces."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mindroom.constants import STREAM_VISIBLE_BODY_KEY, STREAM_WARMUP_SUFFIX_KEY

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping


def _sender_is_trusted(sender_id: object, *, trusted_sender_ids: Collection[str]) -> bool:
    """Return whether one sender may override canonical visible-body reads."""
    return isinstance(sender_id, str) and sender_id in trusted_sender_ids


def _strip_explicit_warmup_suffix(body: str, *, warmup_suffix: str) -> str:
    """Remove one explicitly recorded trailing warmup suffix from the visible body."""
    if not warmup_suffix:
        return body
    if body == warmup_suffix:
        return ""
    joined_suffix = f"\n\n{warmup_suffix}"
    if body.endswith(joined_suffix):
        return body[: -len(joined_suffix)].rstrip()
    return body


def visible_body_from_content(
    content: Mapping[str, object],
    fallback_body: str,
    *,
    sender_id: object,
    trusted_sender_ids: Collection[str] = (),
) -> str:
    """Return the canonical visible body for one content dict."""
    sender_is_trusted = _sender_is_trusted(sender_id, trusted_sender_ids=trusted_sender_ids)
    visible_body = content.get(STREAM_VISIBLE_BODY_KEY)
    if sender_is_trusted and isinstance(visible_body, str) and visible_body:
        return visible_body

    body = content.get("body")
    resolved_body = body if isinstance(body, str) else fallback_body
    if not sender_is_trusted:
        return resolved_body

    warmup_suffix = content.get(STREAM_WARMUP_SUFFIX_KEY)
    if isinstance(warmup_suffix, str) and warmup_suffix:
        return _strip_explicit_warmup_suffix(resolved_body, warmup_suffix=warmup_suffix)
    return resolved_body


def has_trusted_stream_body_metadata(content: Mapping[str, object]) -> bool:
    """Return whether content carries explicit canonical-body metadata."""
    return STREAM_VISIBLE_BODY_KEY in content or STREAM_WARMUP_SUFFIX_KEY in content


def visible_body_from_event_source(
    event_source: Mapping[str, object],
    fallback_body: str,
    *,
    trusted_sender_ids: Collection[str] = (),
) -> str:
    """Return the canonical visible body from one Matrix event source."""
    content = event_source.get("content")
    content_dict = cast("dict[str, object]", content) if isinstance(content, dict) else {}
    new_content = content_dict.get("m.new_content")
    visible_content = cast("dict[str, object]", new_content) if isinstance(new_content, dict) else content_dict
    return visible_body_from_content(
        visible_content,
        fallback_body,
        sender_id=event_source.get("sender"),
        trusted_sender_ids=trusted_sender_ids,
    )


def _visible_preview_content(event_source: object) -> tuple[object, dict[str, object] | None]:
    """Return one sender/content pair suitable for bundled preview resolution."""
    if not isinstance(event_source, dict):
        return None, None
    event_source_dict = cast("dict[str, object]", event_source)
    sender_id = event_source_dict.get("sender")
    content = event_source_dict.get("content")
    if not isinstance(content, dict):
        return sender_id, None
    content_dict = cast("dict[str, object]", content)
    visible_content = (
        content_dict.get("m.new_content") if isinstance(content_dict.get("m.new_content"), dict) else content_dict
    )
    return sender_id, cast("dict[str, object]", visible_content) if isinstance(visible_content, dict) else None


def bundled_visible_body_preview(
    event_source: object,
    *,
    trusted_sender_ids: Collection[str] = (),
) -> str | None:
    """Return one trusted visible body for a bundled replacement candidate."""
    sender_id, visible_content = _visible_preview_content(event_source)
    if not isinstance(visible_content, dict):
        return None
    body = visible_body_from_content(
        visible_content,
        "",
        sender_id=sender_id,
        trusted_sender_ids=trusted_sender_ids,
    )
    has_explicit_body = isinstance(visible_content.get("body"), str)
    if has_explicit_body or has_trusted_stream_body_metadata(visible_content):
        return body
    return None
