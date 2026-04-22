"""Trusted canonical visible-body helpers for Matrix message surfaces."""

from __future__ import annotations

from typing import cast

from mindroom.constants import STREAM_STATUS_KEY, STREAM_VISIBLE_BODY_KEY
from mindroom.matrix.identity import MatrixID


def local_agent_domain_from_user_id(user_id: str | None) -> str | None:
    """Return the local agent domain derived from one Matrix user ID."""
    if not isinstance(user_id, str):
        return None
    try:
        return MatrixID.parse(user_id).domain
    except ValueError:
        return None


def trusted_local_agent_sender(sender_id: object, *, local_agent_domain: str | None) -> bool:
    """Return whether one sender is a trusted local MindRoom agent identity."""
    if not isinstance(sender_id, str) or local_agent_domain is None:
        return False
    try:
        sender = MatrixID.parse(sender_id)
    except ValueError:
        return False
    return sender.domain == local_agent_domain and sender.username.startswith(MatrixID.AGENT_PREFIX)


def _is_worker_warmup_line(line: str) -> bool:
    """Return whether one line is streamed worker warmup decoration."""
    normalized_line = line.strip()
    return normalized_line.startswith(("⏳ Preparing isolated worker", "⚠️ Worker startup failed"))


def _strip_trailing_worker_warmup_block(body: str) -> str:
    """Strip one trailing worker warmup block from a trusted streamed body."""
    sections = body.split("\n\n")
    if len(sections) < 2:
        return body
    trailing_lines = [line for line in sections[-1].splitlines() if line.strip()]
    if trailing_lines and all(_is_worker_warmup_line(line) for line in trailing_lines):
        return "\n\n".join(sections[:-1]).rstrip()
    return body


def visible_body_from_content(
    content: dict[str, object],
    fallback_body: str,
    *,
    sender_id: object,
    local_agent_domain: str | None,
) -> str:
    """Return the canonical visible body for one content dict."""
    trust_stream_visible_body = trusted_local_agent_sender(sender_id, local_agent_domain=local_agent_domain)
    visible_body = content.get(STREAM_VISIBLE_BODY_KEY)
    if trust_stream_visible_body and isinstance(visible_body, str) and visible_body:
        return visible_body
    body = content.get("body")
    if not isinstance(body, str):
        return fallback_body
    if trust_stream_visible_body and isinstance(content.get(STREAM_STATUS_KEY), str):
        return _strip_trailing_worker_warmup_block(body)
    return body


def has_trusted_stream_body_metadata(content: dict[str, object]) -> bool:
    """Return whether content carries canonical-body metadata for trusted streamed text."""
    return STREAM_VISIBLE_BODY_KEY in content or isinstance(content.get(STREAM_STATUS_KEY), str)


def visible_body_from_event_source(
    event_source: dict[str, object],
    fallback_body: str,
    *,
    local_agent_domain: str | None = None,
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
        local_agent_domain=local_agent_domain,
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


def bundled_visible_body_preview(event_source: object, *, local_agent_domain: str | None) -> str | None:
    """Return one trusted visible body for a bundled replacement candidate."""
    sender_id, visible_content = _visible_preview_content(event_source)
    if not isinstance(visible_content, dict):
        return None
    body = visible_body_from_content(
        visible_content,
        "",
        sender_id=sender_id,
        local_agent_domain=local_agent_domain,
    )
    return body or None
