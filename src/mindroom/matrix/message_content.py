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

import asyncio
import copy
import json
import math
from collections import OrderedDict
from dataclasses import dataclass, replace
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING, Any

import nio
from nio import crypto

from mindroom.logging_config import get_logger
from mindroom.matrix.media import MxcUnavailable, download_bounded_mxc_bytes
from mindroom.matrix.sidecar_content import sidecar_content_to_resolve, sidecar_mxc_url
from mindroom.matrix.visible_body import has_trusted_stream_body_metadata, visible_body_from_content

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

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
# The writer uploads one sidecar per message, and the nested edits scripts/utilities/repair_nested_sidecars.py
# repairs hold one more, so a chain longer than two can only come from a crafted event.
_MAX_SIDECAR_HOPS = 2
# Failed sidecars are remembered per process, so every agent in a room and every read does not fetch them again.
# Plaintext is never kept: it belongs to the visible revision the projection stores.
_SIDECAR_MEMORY_SIZE = 1024
_TRANSIENT_UNAVAILABLE_SECONDS = 30.0

# One sidecar reference: the media URL and the canonical JSON of the event's own `file` dict, or None without one.
# Everything that decides whether the payload decrypts and parses is in it, so one sender's reference cannot decide
# the outcome of another's.
type _SidecarReference = tuple[str, str | None]


@dataclass(frozen=True, slots=True)
class _RememberedFailure:
    permanent: bool
    retry_at: float


# Media URLs the homeserver answered as missing or too large, which holds for every reference to them.
_unavailable_media_urls: OrderedDict[str, None] = OrderedDict()
# Failures of whole references: a content failure is permanent for that exact reference, others retry after a pause.
_reference_failures: OrderedDict[_SidecarReference, _RememberedFailure] = OrderedDict()
# One download chain per reference at a time; concurrent readers await the same attempt.
_inflight_chains: dict[_SidecarReference, asyncio.Task[_SidecarChain]] = {}


def _remember_bounded[Key, Value](memory: OrderedDict[Key, Value], key: Key, value: Value) -> None:
    memory[key] = value
    memory.move_to_end(key)
    while len(memory) > _SIDECAR_MEMORY_SIZE:
        memory.popitem(last=False)


def _remembered_failure(reference: _SidecarReference) -> MxcUnavailable | None:
    """Return a remembered failure of this reference, or nothing when it should be downloaded."""
    if reference[0] in _unavailable_media_urls:
        return MxcUnavailable(permanent=True)
    entry = _reference_failures.get(reference)
    if entry is None or (not entry.permanent and monotonic() >= entry.retry_at):
        return None
    return MxcUnavailable(permanent=entry.permanent)


def _remember_chain_outcome(reference: _SidecarReference, unavailable: MxcUnavailable | None) -> None:
    if unavailable is None:
        _reference_failures.pop(reference, None)
        return
    retry_at = math.inf if unavailable.permanent else monotonic() + _TRANSIENT_UNAVAILABLE_SECONDS
    _remember_bounded(_reference_failures, reference, _RememberedFailure(unavailable.permanent, retry_at))


@dataclass(frozen=True, slots=True)
class SidecarResolution:
    """One event's canonical content, or its preview and why the sidecar behind it could not be read.

    ``permanently_unavailable`` means that event's own reference can never resolve: its media is missing or too
    large, or the payload does not decrypt with its key, is not UTF-8, is not a message payload, or chains too far.
    ``failed_download`` means this call downloaded something that failed, rather than reusing a remembered outcome.
    """

    content: dict[str, Any]
    changed: bool
    permanently_unavailable: bool
    failed_download: bool


@dataclass(frozen=True, slots=True)
class _SidecarChain:
    # The last content the chain reached, shared between concurrent readers of one reference and never mutated.
    content: dict[str, Any]
    changed: bool
    unavailable: MxcUnavailable | None
    failed_download: bool


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
    sidecar = await resolve_sidecar_content(event_source.get("content", {}), client)
    return sidecar.content, sidecar.changed


async def resolve_sidecar_content(content: object, client: nio.AsyncClient | None) -> SidecarResolution:
    """Resolve one event content's long-text sidecar chain and say whether an unresolved one can never resolve."""
    preview_content = _normalized_content_dict(content)
    chain = await _resolve_canonical_content(preview_content, client)
    resolved_content = (
        _with_event_relation(copy.deepcopy(chain.content), preview_content) if chain.changed else preview_content
    )
    return SidecarResolution(
        content=resolved_content,
        changed=chain.changed,
        permanently_unavailable=chain.unavailable is not None and chain.unavailable.permanent,
        failed_download=chain.failed_download,
    )


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


async def _download_mxc_text(
    client: nio.AsyncClient,
    mxc_url: str,
    file_info: dict[str, Any] | None = None,
) -> str | MxcUnavailable:
    """Download the text content behind one MXC reference, or say why it cannot be read.

    A payload that cannot decrypt with this reference's key, exceeds the limit once decrypted, or is not UTF-8 never
    will. Missing or oversized media is remembered by URL, because that holds for every reference to it.
    """
    download = await download_bounded_mxc_bytes(client, mxc_url, max_bytes=_MXC_TEXT_MAX_BYTES)
    if isinstance(download, MxcUnavailable):
        if download.permanent:
            _remember_bounded(_unavailable_media_urls, mxc_url, None)
        return download
    text_bytes = download.data
    if file_info and "key" in file_info:
        try:
            text_bytes = crypto.attachments.decrypt_attachment(
                text_bytes,
                file_info["key"]["k"],
                file_info["hashes"]["sha256"],
                file_info["iv"],
            )
        except Exception:
            logger.exception("Failed to decrypt attachment", mxc_url=mxc_url)
            return MxcUnavailable(permanent=True)
        if not isinstance(text_bytes, bytes):
            logger.error("mxc_decrypt_returned_non_bytes_payload", mxc_url=mxc_url)
            return MxcUnavailable(permanent=True)
        if _mxc_bytes_exceed_limit(mxc_url, text_bytes, stage="decrypt"):
            return MxcUnavailable(permanent=True)
    try:
        return text_bytes.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("mxc_text_payload_not_utf8", mxc_url=mxc_url)
        return MxcUnavailable(permanent=True)


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
) -> _SidecarChain:
    """Follow bounded v2 sidecar chains, retaining unresolved content and why on failure."""
    first_sidecar = sidecar_content_to_resolve(content)
    first_mxc_url = None if first_sidecar is None else sidecar_mxc_url(first_sidecar)
    if client is None or first_sidecar is None or first_mxc_url is None:
        return _SidecarChain(content=content, changed=False, unavailable=None, failed_download=False)
    reference = (first_mxc_url, _canonical_file_info(first_sidecar))
    if (remembered := _remembered_failure(reference)) is not None:
        return _SidecarChain(content=content, changed=False, unavailable=remembered, failed_download=False)
    loop = asyncio.get_running_loop()
    joined = _inflight_chains.get(reference)
    if joined is not None and joined.get_loop() is loop:
        # A reader that joins another's attempt downloads nothing itself.
        return replace(await asyncio.shield(joined), failed_download=False)
    task = loop.create_task(_download_and_remember_chain(reference, content, client))
    _inflight_chains[reference] = task
    task.add_done_callback(partial(_forget_inflight_chain, reference))
    return await asyncio.shield(task)


def _canonical_file_info(sidecar_content: Mapping[str, Any]) -> str | None:
    file_info = sidecar_content.get("file")
    if not isinstance(file_info, dict):
        return None
    return json.dumps(file_info, sort_keys=True, separators=(",", ":"), default=repr)


def _forget_inflight_chain(reference: _SidecarReference, task: asyncio.Task[_SidecarChain]) -> None:
    if _inflight_chains.get(reference) is task:
        del _inflight_chains[reference]


async def _download_and_remember_chain(
    reference: _SidecarReference,
    content: dict[str, Any],
    client: nio.AsyncClient,
) -> _SidecarChain:
    chain = await _download_sidecar_chain(content, client)
    _remember_chain_outcome(reference, chain.unavailable)
    return chain


async def _download_sidecar_chain(content: dict[str, Any], client: nio.AsyncClient) -> _SidecarChain:
    visited: set[str] = set()
    for hop in range(_MAX_SIDECAR_HOPS + 1):
        sidecar_content = sidecar_content_to_resolve(content)
        if sidecar_content is None:
            return _SidecarChain(content=content, changed=hop > 0, unavailable=None, failed_download=False)
        mxc_url = sidecar_mxc_url(sidecar_content)
        if mxc_url is None or mxc_url in visited or hop == _MAX_SIDECAR_HOPS:
            logger.warning("mxc_sidecar_chain_unresolvable", mxc_url=mxc_url, hops=hop)
            return _unresolved_chain(content, hop, MxcUnavailable(permanent=True), failed_download=False)
        if mxc_url in _unavailable_media_urls:
            return _unresolved_chain(content, hop, MxcUnavailable(permanent=True), failed_download=False)
        visited.add(mxc_url)
        file_info = sidecar_content.get("file")
        try:
            full_text = await _download_mxc_text(client, mxc_url, file_info if isinstance(file_info, dict) else None)
        except Exception:
            logger.exception("Error downloading MXC content", mxc_url=mxc_url)
            full_text = MxcUnavailable(permanent=False)
        if isinstance(full_text, MxcUnavailable):
            return _unresolved_chain(content, hop, full_text, failed_download=True)
        resolved_content = _extract_large_message_v2_content(full_text)
        if resolved_content is None:
            logger.warning("Invalid large-message v2 payload JSON, returning preview content", mxc_url=mxc_url)
            return _unresolved_chain(content, hop, MxcUnavailable(permanent=True), failed_download=True)
        content = resolved_content
    msg = "Sidecar chain ended without an outcome"
    raise AssertionError(msg)


def _unresolved_chain(
    content: dict[str, Any],
    hop: int,
    unavailable: MxcUnavailable,
    *,
    failed_download: bool,
) -> _SidecarChain:
    return _SidecarChain(content=content, changed=hop > 0, unavailable=unavailable, failed_download=failed_download)
