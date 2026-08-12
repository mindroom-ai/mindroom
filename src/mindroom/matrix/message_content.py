"""Centralized message content extraction for Matrix sidecar-backed messages.

A message too large for one Matrix event carries a truncated preview in its
content and its real text in an attached file. This module resolves that file.

It keeps no durable memory of what it resolved, and must not acquire one. The
resolved text belongs to the visible revision it is the body of, and that is
stored once, in ``visible_messages.content_json``: an edit, a redaction, or a
membership epoch advance all replace or remove the row, so the resolution is
invalidated by the projection already working. A second durable copy keyed by
MXC reference -- which is what this module used to keep in the event cache --
needs its own invalidation and its own redaction cleanup to stay honest, and a
plaintext store that misses a redaction serves deleted content.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import nio
from nio import crypto

from mindroom.logging_config import get_logger
from mindroom.matrix.sidecar_content import sidecar_content_to_resolve, sidecar_mxc_url
from mindroom.matrix.visible_body import has_trusted_stream_body_metadata, visible_body_from_content

if TYPE_CHECKING:
    from collections.abc import Collection

logger = get_logger(__name__)

# Every `m.room.message` nio could type, which is exactly every one carrying a
# `body`. `RoomMessageFormatted`, `RoomMessageMedia`, and `RoomEncryptedMedia`
# are the three direct children of `nio.RoomMessage` that declare one, and
# `RoomMessageUnknown` is the fourth and declares none.
#
# Written as a union only because nio gives encrypted and unencrypted media two
# sibling bases instead of one, so no single class spans them. The rule that
# decides membership at runtime is deliberately not this list:
# `client_visible_messages.is_visible_room_message` asks the base class and
# names the one exclusion, because four separate curated lists of `RoomMessage`
# children have now each dropped a msgtype after shipping.
type VisibleRoomMessage = nio.RoomMessageFormatted | nio.RoomMessageMedia | nio.RoomEncryptedMedia

_MXC_TEXT_MAX_BYTES = 2 * 1024 * 1024


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


def _with_event_relation(
    resolved_content: dict[str, Any],
    event_content: dict[str, Any],
) -> dict[str, Any]:
    """Return hydrated content that still sits where the event sits.

    A sidecar carries the text that did not fit in the event, and nothing else.
    ``m.relates_to`` is the whole of an event's position in the relation graph --
    thread, edit, reply, reaction and reference are all read out of it -- and
    that position is a property of the event the server stored, not of a file
    the event points at. Letting a downloaded payload restate it would hand
    whoever uploaded the file the choice of which conversation the message
    joins, and would leave the journal, which records the event's relation, at
    odds with the turn, which reads the payload's.

    So the event's relation is restored over whatever the payload named,
    including when the event has none. On the honest path the two already agree,
    because a large message uploads its own outer content.
    """
    relation = event_content.get("m.relates_to")
    if relation is None:
        resolved_content.pop("m.relates_to", None)
    else:
        resolved_content["m.relates_to"] = relation
    return resolved_content


async def _resolve_event_content(
    event_source: dict[str, Any],
    client: nio.AsyncClient | None,
) -> tuple[dict[str, Any], bool]:
    """Return one event's canonical content plus whether resolving it changed anything."""
    preview_content = _normalized_content_dict(event_source.get("content", {}))
    resolved_content = await _resolve_canonical_content(preview_content, client)
    if resolved_content is preview_content:
        return preview_content, False
    return _with_event_relation(resolved_content, preview_content), True


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


async def _download_mxc_text(  # noqa: PLR0911, PLR0912, C901
    client: nio.AsyncClient,
    mxc_url: str,
    file_info: dict[str, Any] | None = None,
) -> str | None:
    """Download the text content behind one MXC reference.

    Args:
        client: Matrix client
        mxc_url: The MXC URL to download from
        file_info: Optional encryption info for E2EE rooms

    Returns:
        The downloaded text content, or None if download failed

    """
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
    except Exception:
        logger.exception("Error downloading MXC content")
        return None
    else:
        return decoded_text


async def extract_and_resolve_message(
    event: VisibleRoomMessage,
    client: nio.AsyncClient | None = None,
    *,
    trusted_sender_ids: Collection[str] = (),
) -> dict[str, Any]:
    """Extract message data and resolve large message content if needed.

    This is a convenience function that combines extraction and resolution
    of large message content in a single call.

    Args:
        event: The Matrix event to extract data from
        client: Optional Matrix client for downloading attachments
        trusted_sender_ids: Exact trusted internal sender IDs allowed to override visible body

    Returns:
        Dict with sender, body, timestamp, event_id, and content fields.
        If the message is large and client is provided, body will contain
        the full text from the attachment.

    """
    resolved_content, _ = await _resolve_event_content(event.source, client)
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
    trusted_sender_ids: Collection[str] = (),
) -> tuple[str | None, dict[str, Any] | None]:
    """Extract body/content from an edit event's ``m.new_content`` payload."""
    resolved_content, _ = await _resolve_event_content(event_source, client)
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
) -> dict[str, Any]:
    """Return an event source with canonical v2 sidecar content hydrated when available."""
    resolved_content, content_changed = await _resolve_event_content(event_source, client)
    if not content_changed:
        return event_source

    resolved_event_source = {key: value for key, value in event_source.items() if isinstance(key, str)}
    resolved_event_source["content"] = resolved_content
    return resolved_event_source


async def _resolve_canonical_content(
    content: dict[str, Any],
    client: nio.AsyncClient | None,
) -> dict[str, Any]:
    """Hydrate canonical event content from a v2 JSON sidecar when available."""
    sidecar_content = sidecar_content_to_resolve(content)
    if client is None or sidecar_content is None:
        return content

    mxc_url = sidecar_mxc_url(sidecar_content)
    if mxc_url is None:
        return content

    full_text = await _download_mxc_text(
        client,
        mxc_url,
        sidecar_content.get("file") if isinstance(sidecar_content.get("file"), dict) else None,
    )
    if full_text is None:
        return content

    resolved_content = _extract_large_message_v2_content(full_text)
    if resolved_content is None:
        logger.warning("Invalid large-message v2 payload JSON, returning preview content")
        return content

    return resolved_content
