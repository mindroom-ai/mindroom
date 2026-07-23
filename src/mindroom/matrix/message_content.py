"""Centralized message content extraction for Matrix sidecar-backed messages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import nio
from nio import crypto

from mindroom.logging_config import get_logger
from mindroom.matrix.membership_fence import UNCERTIFIED_MEMBERSHIP_EPOCH
from mindroom.matrix.sidecar_content import sidecar_mxc_url
from mindroom.matrix.visible_body import has_trusted_stream_body_metadata, visible_body_from_content

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from mindroom.matrix.cache import ConversationEventCache

logger = get_logger(__name__)

_MXC_TEXT_MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SidecarHydrationBatch:
    """Request-scoped durable sidecar hits and their visible owner event IDs."""

    cached_texts: Mapping[tuple[str, str], str]
    owner_event_ids: frozenset[str]


def _extract_large_message_v2_content(payload_json: str) -> dict[str, Any] | None:
    """Extract canonical content dict from a v2 large-message sidecar JSON payload."""
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return {key: value for key, value in payload.items() if isinstance(key, str)}


def _normalized_content_dict(content: object) -> dict[str, Any]:
    """Return a string-keyed content dict."""
    if not isinstance(content, dict):
        return {}
    return {key: value for key, value in content.items() if isinstance(key, str)}


def is_v2_sidecar_text_preview(event_source: dict[str, Any]) -> bool:
    """Return whether one event source is a large-text preview transported as ``m.file``."""
    content = _normalized_content_dict(event_source.get("content", {}))
    if content.get("msgtype") != "m.file":
        return False

    return sidecar_mxc_url(content) is not None


def _sidecar_content_for_resolution(content: dict[str, Any]) -> dict[str, Any] | None:
    """Return the content dict that owns the long-text sidecar metadata."""
    if "io.mindroom.long_text" in content:
        return content

    new_content = content.get("m.new_content")
    if isinstance(new_content, dict) and "io.mindroom.long_text" in new_content:
        return new_content

    return None


def _sidecar_reference(event_source: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return one visible event's exact durable sidecar reference."""
    event_id = event_source.get("event_id")
    content = _normalized_content_dict(event_source.get("content"))
    sidecar_content = _sidecar_content_for_resolution(content)
    mxc_url = sidecar_mxc_url(sidecar_content) if sidecar_content is not None else None
    if not isinstance(event_id, str) or not event_id or mxc_url is None:
        return None
    return event_id, mxc_url


async def prepare_sidecar_hydration_batch(
    event_sources: Sequence[dict[str, Any]],
    *,
    event_cache: ConversationEventCache,
    room_id: str,
    expected_membership_epoch: int | None,
    register_owners: bool,
) -> SidecarHydrationBatch | None:
    """Prepare one bounded durable lookup for all sidecars in a history reconstruction."""
    if expected_membership_epoch is None or expected_membership_epoch == UNCERTIFIED_MEMBERSHIP_EPOCH:
        return None
    source_by_event_id = {
        reference[0]: event_source
        for event_source in event_sources
        if (reference := _sidecar_reference(event_source)) is not None
    }
    references = tuple(
        reference
        for event_source in source_by_event_id.values()
        if (reference := _sidecar_reference(event_source)) is not None
    )
    if not references:
        return None
    if register_owners:
        try:
            await event_cache.store_events_batch(
                [(event_id, room_id, source_by_event_id[event_id]) for event_id, _mxc_url in references],
                expected_membership_epoch=expected_membership_epoch,
            )
        except Exception:
            logger.exception(
                "Failed to batch-register long-text sidecar ownership",
                room_id=room_id,
                event_count=len(references),
            )
            return None
    try:
        cached_texts = await event_cache.get_mxc_texts(
            room_id,
            references,
            expected_membership_epoch=expected_membership_epoch,
        )
    except Exception:
        logger.exception(
            "Failed to batch-read durable MXC text cache",
            room_id=room_id,
            reference_count=len(references),
        )
        return None
    return SidecarHydrationBatch(
        cached_texts=cached_texts,
        owner_event_ids=frozenset(event_id for event_id, _mxc_url in references),
    )


async def _register_sidecar_owner(
    event_source: dict[str, Any],
    *,
    event_cache: ConversationEventCache | None,
    room_id: str | None,
    fallback_event_id: str | None = None,
    expected_membership_epoch: int | None = None,
    hydration_batch: SidecarHydrationBatch | None = None,
) -> str | None:
    """Persist the visible event/reference before plaintext hydration begins."""
    content = _normalized_content_dict(event_source.get("content"))
    sidecar_content = _sidecar_content_for_resolution(content)
    if sidecar_content is None or sidecar_mxc_url(sidecar_content) is None:
        event_id = event_source.get("event_id")
        return event_id if isinstance(event_id, str) else fallback_event_id
    event_id_value = event_source.get("event_id")
    event_id = event_id_value if isinstance(event_id_value, str) and event_id_value else fallback_event_id
    if (hydration_batch is not None and event_id in hydration_batch.owner_event_ids) or event_cache is None:
        return event_id
    if room_id is None or event_id is None:
        return None
    if expected_membership_epoch is None or expected_membership_epoch == UNCERTIFIED_MEMBERSHIP_EPOCH:
        return event_id
    owned_event = dict(event_source)
    owned_event["event_id"] = event_id
    try:
        if await event_cache.get_event(room_id, event_id) is None:
            await event_cache.store_event(
                event_id,
                room_id,
                owned_event,
                expected_membership_epoch=expected_membership_epoch,
            )
    except Exception:
        logger.exception(
            "Failed to register long-text sidecar ownership",
            room_id=room_id,
            event_id=event_id,
        )
        return None
    return event_id


async def _resolve_event_content(
    event_source: dict[str, Any],
    client: nio.AsyncClient | None,
    *,
    event_cache: ConversationEventCache | None,
    room_id: str | None,
    fallback_event_id: str | None = None,
    expected_membership_epoch: int | None = None,
    hydration_batch: SidecarHydrationBatch | None = None,
) -> tuple[dict[str, Any], bool]:
    """Register valid sidecar ownership and return canonical content plus whether it changed."""
    preview_content = _normalized_content_dict(event_source.get("content", {}))
    event_id = await _register_sidecar_owner(
        event_source,
        event_cache=event_cache,
        room_id=room_id,
        fallback_event_id=fallback_event_id,
        expected_membership_epoch=expected_membership_epoch,
        hydration_batch=hydration_batch,
    )
    resolved_content = await _resolve_canonical_content(
        preview_content,
        client,
        event_cache=event_cache,
        room_id=room_id,
        event_id=event_id,
        expected_membership_epoch=expected_membership_epoch,
        hydration_batch=hydration_batch,
    )
    return resolved_content, resolved_content is not preview_content


def _text_size_bytes(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _mxc_bytes_exceed_limit(mxc_url: str, payload: bytes, *, stage: str) -> bool:
    if len(payload) <= _MXC_TEXT_MAX_BYTES:
        return False
    logger.warning(
        "mxc_text_payload_exceeds_byte_limit",
        mxc_url=mxc_url,
        stage=stage,
        size_bytes=len(payload),
        limit_bytes=_MXC_TEXT_MAX_BYTES,
    )
    return True


async def _download_mxc_text(  # noqa: PLR0911, PLR0912, PLR0915, C901
    client: nio.AsyncClient,
    mxc_url: str,
    file_info: dict[str, Any] | None = None,
    *,
    event_cache: ConversationEventCache | None = None,
    room_id: str | None = None,
    event_id: str | None = None,
    expected_membership_epoch: int | None = None,
    hydration_batch: SidecarHydrationBatch | None = None,
) -> str | None:
    """Download text content from an MXC URL with caching.

    Args:
        client: Matrix client
        mxc_url: The MXC URL to download from
        file_info: Optional encryption info for E2EE rooms
        event_cache: Optional durable event cache used for restart-safe MXC text reuse
        room_id: Room scope for event-cache locking when a durable MXC cache is available
        event_id: Visible event that owns the room-scoped MXC reference
        expected_membership_epoch: Durable room transition expected by fetch-derived writes
        hydration_batch: Request-scoped durable sidecar hits from one bounded cache read
    Returns:
        The downloaded text content, or None if download failed

    """
    cache_writes_certified = (
        expected_membership_epoch is not None and expected_membership_epoch != UNCERTIFIED_MEMBERSHIP_EPOCH
    )
    if cache_writes_certified and event_cache is not None and room_id is not None and event_id is not None:
        if hydration_batch is not None:
            cached_text = hydration_batch.cached_texts.get((event_id, mxc_url))
        else:
            try:
                cached_text = await event_cache.get_mxc_text(room_id, event_id, mxc_url)
            except Exception:
                logger.exception("Failed to read durable MXC text cache")
                cached_text = None
        if cached_text is not None:
            if _text_size_bytes(cached_text) > _MXC_TEXT_MAX_BYTES:
                logger.warning(
                    "durable_mxc_text_cache_entry_exceeds_byte_limit",
                    mxc_url=mxc_url,
                    room_id=room_id,
                    size_bytes=_text_size_bytes(cached_text),
                    limit_bytes=_MXC_TEXT_MAX_BYTES,
                )
                return None
            logger.debug("mxc_text_cache_hit", mxc_url=mxc_url, room_id=room_id)
            return cached_text

    try:
        # Parse MXC URL
        if not mxc_url.startswith("mxc://"):
            logger.error("invalid_mxc_url", mxc_url=mxc_url)
            return None

        # Validate the MXC URL structure before issuing the download.
        parts = mxc_url[6:].split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            logger.error("invalid_mxc_url_format", mxc_url=mxc_url)
            return None

        response = await client.download(mxc=mxc_url)

        if not isinstance(response, nio.DownloadResponse):
            logger.error("mxc_download_failed", mxc_url=mxc_url, error=str(response))
            return None
        if not isinstance(response.body, bytes):
            logger.error("mxc_download_returned_non_bytes_payload", mxc_url=mxc_url)
            return None
        if _mxc_bytes_exceed_limit(mxc_url, response.body, stage="download"):
            return None

        # Handle encryption if needed
        if file_info and "key" in file_info:
            # Decrypt the content
            try:
                decrypted = crypto.attachments.decrypt_attachment(
                    response.body,
                    file_info["key"]["k"],
                    file_info["hashes"]["sha256"],
                    file_info["iv"],
                )
                text_bytes = decrypted
            except Exception:
                logger.exception("Failed to decrypt attachment")
                return None
            if not isinstance(text_bytes, bytes):
                logger.error("mxc_decrypt_returned_non_bytes_payload", mxc_url=mxc_url)
                return None
            if _mxc_bytes_exceed_limit(mxc_url, text_bytes, stage="decrypt"):
                return None
        else:
            text_bytes = response.body

        # Decode to text
        try:
            decoded_text: str = text_bytes.decode("utf-8")
        except UnicodeDecodeError:
            logger.exception("Downloaded content is not valid UTF-8 text")
            return None
        if cache_writes_certified and event_cache is not None and room_id is not None and event_id is not None:
            try:
                ownership_persisted = await event_cache.store_mxc_text(
                    room_id,
                    event_id,
                    mxc_url,
                    decoded_text,
                    expected_membership_epoch=expected_membership_epoch,
                )
            except Exception:
                logger.exception("Failed to persist durable MXC text cache")
            else:
                if not ownership_persisted:
                    if not event_cache.durable_writes_available:
                        logger.info(
                            "mxc_plaintext_returned_uncached_cache_unavailable",
                            room_id=room_id,
                            event_id=event_id,
                        )
                        return decoded_text
                    logger.info(
                        "mxc_plaintext_rejected_without_visible_owner",
                        room_id=room_id,
                        event_id=event_id,
                    )
                    return None

    except Exception:
        logger.exception("Error downloading MXC content")
        return None
    else:
        return decoded_text


async def extract_and_resolve_message(
    event: nio.RoomMessageText | nio.RoomMessageNotice,
    client: nio.AsyncClient | None = None,
    *,
    event_cache: ConversationEventCache | None = None,
    room_id: str | None = None,
    expected_membership_epoch: int | None = None,
    hydration_batch: SidecarHydrationBatch | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> dict[str, Any]:
    """Extract message data and resolve large message content if needed.

    This is a convenience function that combines extraction and resolution
    of large message content in a single call.

    Args:
        event: The Matrix event to extract data from
        client: Optional Matrix client for downloading attachments
        event_cache: Optional durable event cache used for restart-safe sidecar reuse
        room_id: Room scope for durable sidecar cache reads and writes
        expected_membership_epoch: Durable room transition expected by fetch-derived writes
        hydration_batch: Request-scoped durable sidecar hits from one bounded cache read
        trusted_sender_ids: Exact trusted internal sender IDs allowed to override visible body

    Returns:
        Dict with sender, body, timestamp, event_id, and content fields.
        If the message is large and client is provided, body will contain
        the full text from the attachment.

    """
    resolved_content, _ = await _resolve_event_content(
        event.source,
        client,
        event_cache=event_cache,
        room_id=room_id,
        fallback_event_id=event.event_id,
        expected_membership_epoch=expected_membership_epoch,
        hydration_batch=hydration_batch,
    )
    resolved_body = visible_body_from_content(
        resolved_content,
        event.body,
        sender_id=event.sender,
        trusted_sender_ids=trusted_sender_ids,
    )
    relates_to = _normalized_content_dict(resolved_content.get("m.relates_to"))
    if event.sender in trusted_sender_ids and relates_to.get("rel_type") == "m.replace":
        new_content = _normalized_content_dict(resolved_content.get("m.new_content"))
        if has_trusted_stream_body_metadata(new_content):
            resolved_body = visible_body_from_content(
                new_content,
                resolved_body,
                sender_id=event.sender,
                trusted_sender_ids=trusted_sender_ids,
            )
    message_data = {
        "sender": event.sender,
        "body": resolved_body,
        "timestamp": event.server_timestamp,
        "event_id": event.event_id,
        "content": resolved_content,
    }
    msgtype = resolved_content.get("msgtype")
    if isinstance(msgtype, str):
        message_data["msgtype"] = msgtype
    return message_data


async def extract_edit_body(
    event_source: dict[str, Any],
    client: nio.AsyncClient | None = None,
    *,
    event_cache: ConversationEventCache | None = None,
    room_id: str | None = None,
    expected_membership_epoch: int | None = None,
    hydration_batch: SidecarHydrationBatch | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> tuple[str | None, dict[str, Any] | None]:
    """Extract body/content from an edit event's ``m.new_content`` payload."""
    resolved_content, _ = await _resolve_event_content(
        event_source,
        client,
        event_cache=event_cache,
        room_id=room_id,
        expected_membership_epoch=expected_membership_epoch,
        hydration_batch=hydration_batch,
    )
    new_content = _normalized_content_dict(resolved_content.get("m.new_content"))
    body = visible_body_from_content(
        new_content,
        "",
        sender_id=event_source.get("sender"),
        trusted_sender_ids=trusted_sender_ids,
    )
    if isinstance(new_content.get("body"), str) or body:
        normalized_new_content = dict(new_content)
        normalized_new_content["body"] = body
        return body, normalized_new_content
    return None, None


async def resolve_event_source_content(
    event_source: dict[str, Any],
    client: nio.AsyncClient | None = None,
    *,
    event_cache: ConversationEventCache | None = None,
    room_id: str | None = None,
    expected_membership_epoch: int | None = None,
    hydration_batch: SidecarHydrationBatch | None = None,
) -> dict[str, Any]:
    """Return an event source with canonical v2 sidecar content hydrated when available."""
    resolved_content, content_changed = await _resolve_event_content(
        event_source,
        client,
        event_cache=event_cache,
        room_id=room_id,
        expected_membership_epoch=expected_membership_epoch,
        hydration_batch=hydration_batch,
    )
    if not content_changed:
        return event_source

    resolved_event_source = {key: value for key, value in event_source.items() if isinstance(key, str)}
    resolved_event_source["content"] = resolved_content
    return resolved_event_source


async def _resolve_canonical_content(
    content: dict[str, Any],
    client: nio.AsyncClient | None,
    *,
    event_cache: ConversationEventCache | None,
    room_id: str | None,
    event_id: str | None,
    expected_membership_epoch: int | None,
    hydration_batch: SidecarHydrationBatch | None,
) -> dict[str, Any]:
    """Hydrate canonical event content from a v2 JSON sidecar when available."""
    sidecar_content = _sidecar_content_for_resolution(content)
    if client is None or sidecar_content is None:
        return content

    mxc_url = sidecar_mxc_url(sidecar_content)
    if mxc_url is None:
        return content

    full_text = await _download_mxc_text(
        client,
        mxc_url,
        sidecar_content.get("file") if isinstance(sidecar_content.get("file"), dict) else None,
        event_cache=event_cache,
        room_id=room_id,
        event_id=event_id,
        expected_membership_epoch=expected_membership_epoch,
        hydration_batch=hydration_batch,
    )
    if full_text is None:
        return content

    resolved_content = _extract_large_message_v2_content(full_text)
    if resolved_content is None:
        logger.warning("Invalid large-message v2 payload JSON, returning preview content")
        return content

    return resolved_content
