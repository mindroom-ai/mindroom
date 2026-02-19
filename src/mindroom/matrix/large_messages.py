"""Handle large Matrix messages that exceed the 64KB event limit.

This module provides minimal intervention for messages that are too large,
uploading the full text as an MXC attachment while maximizing the preview size.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any

import nio
from nio import crypto

from mindroom.logging_config import get_logger

logger = get_logger(__name__)

# Conservative limits accounting for Matrix overhead
NORMAL_MESSAGE_LIMIT = 55000  # ~55KB for regular messages
EDIT_MESSAGE_LIMIT = 27000  # ~27KB for edits (they roughly double in size)
PASSTHROUGH_CONTENT_KEYS = ("m.mentions", "com.mindroom.skip_mentions")

_TOOL_BLOCK_RE = re.compile(r"<tool>.*?</tool>", re.DOTALL)
_TOOL_TRUNCATION_MARKER = "\n[…]\n"


def _calculate_event_size(content: dict[str, Any]) -> int:
    """Calculate the approximate size of a Matrix event.

    Args:
        content: The message content dictionary

    Returns:
        Approximate size in bytes including JSON overhead

    """
    # Convert to canonical JSON (sorted keys, no spaces)
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    # Add ~2KB overhead for event metadata, signatures, etc.
    return len(canonical.encode("utf-8")) + 2000


def _is_edit_message(content: dict[str, Any]) -> bool:
    """Check if this is an edit message."""
    return "m.new_content" in content or (
        "m.relates_to" in content and content.get("m.relates_to", {}).get("rel_type") == "m.replace"
    )


def _prefix_by_bytes(text: str, max_bytes: int) -> str:
    """Return the longest prefix of *text* that fits within *max_bytes* UTF-8."""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    lo, hi, best = 0, min(len(text), max_bytes), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(text[:mid].encode("utf-8")) <= max_bytes:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return text[:best]


def _suffix_by_bytes(text: str, max_bytes: int) -> str:
    """Return the longest suffix of *text* that fits within *max_bytes* UTF-8."""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    lo, hi, best = 0, len(text), len(text)
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(text[mid:].encode("utf-8")) <= max_bytes:
            best = mid
            hi = mid - 1
        else:
            lo = mid + 1
    return text[best:]


_CONTINUATION_INDICATOR = "\n\n[Message continues in attached file]"


def _create_preview(text: str, max_bytes: int) -> str:
    """Create a preview that fits within byte limit.

    Args:
        text: The full text to preview
        max_bytes: Maximum size in bytes for the preview

    Returns:
        Preview text that fits within the byte limit

    """
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    indicator_bytes = len(_CONTINUATION_INDICATOR.encode("utf-8"))
    target_bytes = max_bytes - indicator_bytes
    if target_bytes <= 0:
        return _CONTINUATION_INDICATOR.lstrip()

    return _prefix_by_bytes(text, target_bytes) + _CONTINUATION_INDICATOR


def _truncate_tool_block(tool_text: str, max_bytes: int) -> str:
    """Truncate a ``<tool>…</tool>`` block, cutting from the middle symmetrically."""
    if len(tool_text.encode("utf-8")) <= max_bytes:
        return tool_text

    tag_open, tag_close = "<tool>", "</tool>"
    marker = _TOOL_TRUNCATION_MARKER
    shell = f"{tag_open}{marker}{tag_close}"
    shell_bytes = len(shell.encode("utf-8"))

    if max_bytes < shell_bytes:
        return ""

    inner = tool_text[len(tag_open) : -len(tag_close)]
    inner_budget = max_bytes - shell_bytes
    if inner_budget <= 0:
        return shell

    half = inner_budget // 2
    start = _prefix_by_bytes(inner, half)
    end = _suffix_by_bytes(inner, inner_budget - len(start.encode("utf-8")))
    return f"{tag_open}{start}{marker}{end}{tag_close}"


def _create_tool_aware_preview(text: str, max_bytes: int) -> str:
    """Create a preview that prioritises non-tool content.

    ``<tool>…</tool>`` blocks are shrunk first (middle-out) so that the
    surrounding human-readable text survives as long as possible.
    Falls back to :func:`_create_preview` when there are no tool blocks
    or when even the non-tool text exceeds the budget.
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    indicator_bytes = len(_CONTINUATION_INDICATOR.encode("utf-8"))
    target = max_bytes - indicator_bytes
    if target <= 0:
        return _CONTINUATION_INDICATOR.lstrip()

    matches = list(_TOOL_BLOCK_RE.finditer(text))
    if not matches:
        return _create_preview(text, max_bytes)

    # Split into (kind, content) segments
    segments: list[tuple[str, str]] = []
    pos = 0
    for m in matches:
        if m.start() > pos:
            segments.append(("text", text[pos : m.start()]))
        segments.append(("tool", m.group()))
        pos = m.end()
    if pos < len(text):
        segments.append(("text", text[pos:]))

    non_tool_bytes = sum(len(content.encode("utf-8")) for kind, content in segments if kind == "text")
    if non_tool_bytes >= target:
        # Non-tool content alone exceeds budget; fall back to simple truncation
        return _create_preview(text, max_bytes)

    tool_budget = target - non_tool_bytes
    tool_entries = [(i, len(segments[i][1].encode("utf-8"))) for i, (kind, _) in enumerate(segments) if kind == "tool"]
    total_tool = sum(size for _, size in tool_entries)

    result_parts = [content for _, content in segments]
    for idx, size in tool_entries:
        alloc = int(tool_budget * size / total_tool) if total_tool else 0
        result_parts[idx] = _truncate_tool_block(segments[idx][1], alloc)

    result = "".join(result_parts)
    # Safety: hard-trim if rounding pushed us over
    return _prefix_by_bytes(result, target) + _CONTINUATION_INDICATOR


async def _upload_text_as_mxc(
    client: nio.AsyncClient,
    text: str,
    room_id: str | None = None,
    *,
    mimetype: str = "text/plain",
) -> tuple[str | None, dict[str, Any] | None]:
    """Upload text content as an MXC file.

    Args:
        client: The Matrix client
        text: The text content to upload
        room_id: Optional room ID to check for encryption
        mimetype: MIME type for the uploaded content (default: "text/plain")

    Returns:
        Tuple of (mxc_uri, file_info_dict) or (None, None) on failure

    """
    text_bytes = text.encode("utf-8")
    file_info = {
        "size": len(text_bytes),
        "mimetype": mimetype,
    }

    is_html = mimetype == "text/html"
    filename = "message.html" if is_html else "message.txt"

    # Check if room is encrypted
    room_encrypted = False
    if room_id and room_id in client.rooms:
        room = client.rooms[room_id]
        room_encrypted = room.encrypted

    if room_encrypted:
        # Encrypt the content for E2EE room
        try:
            upload_data, encryption_keys = crypto.attachments.encrypt_attachment(text_bytes)

            # Store encryption info for the file
            file_info = {
                "url": "",  # Will be set after upload
                "key": encryption_keys["key"],
                "iv": encryption_keys["iv"],
                "hashes": encryption_keys["hashes"],
                "v": "v2",
                "mimetype": mimetype,
                "size": len(text_bytes),
            }
        except Exception:
            logger.exception("Failed to encrypt attachment")
            return None, None
    else:
        upload_data = text_bytes

    # Upload the file
    def data_provider(_monitor: object, _data: object) -> io.BytesIO:
        return io.BytesIO(upload_data)

    enc_filename = f"{filename}.enc" if room_encrypted else filename

    try:
        # nio.upload returns Tuple[Union[UploadResponse, UploadError], Optional[Dict[str, Any]]]
        upload_result, encryption_dict = await client.upload(
            data_provider=data_provider,
            content_type="application/octet-stream" if room_encrypted else mimetype,
            filename=enc_filename,
            filesize=len(upload_data),
        )

        # Check if upload was successful
        if not isinstance(upload_result, nio.UploadResponse):
            logger.error(f"Failed to upload text: {upload_result}")
            return None, None

        if not upload_result.content_uri:
            logger.error("Upload response missing content_uri")
            return None, None

        mxc_uri = str(upload_result.content_uri)
        file_info["url"] = mxc_uri

    except Exception:
        logger.exception("Failed to upload text")
        return None, None
    else:
        return mxc_uri, file_info


async def _build_file_content(
    client: nio.AsyncClient,
    room_id: str,
    source_content: dict[str, Any],
    full_text: str,
    has_tool_html: bool,
    size_limit: int,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any]]:
    """Upload the full text and build the ``m.file`` content dict with previews.

    When *has_tool_html* is True the formatted_body (HTML) is uploaded so the
    Element fork can render ``<tool>`` as real HTML elements.  Otherwise plain
    text is uploaded so that thread-history replay feeds clean text into AI
    prompts instead of raw HTML markup.
    """
    if has_tool_html:
        upload_text = source_content["formatted_body"]
        upload_mimetype = "text/html"
    else:
        upload_text = full_text
        upload_mimetype = "text/plain"

    mxc_uri, file_info = await _upload_text_as_mxc(client, upload_text, room_id, mimetype=upload_mimetype)

    # When tool-HTML is present both body and formatted_body are included in
    # the event, so each preview gets half the budget to stay under the limit.
    attachment_overhead = 5000  # Conservative estimate for attachment JSON structure
    available = (size_limit - attachment_overhead) // 2 if has_tool_html else size_limit - attachment_overhead

    preview_fn = _create_tool_aware_preview if has_tool_html else _create_preview
    preview = preview_fn(full_text, available)

    modified_content: dict[str, Any] = {
        "msgtype": "m.file",
        "body": preview,
        "filename": "message.html" if has_tool_html else "message.txt",
        "info": file_info,
    }

    # Preserve HTML format metadata so the Element fork treats the downloaded
    # file as HTML and renders <tool> elements via the collapsible renderer.
    if has_tool_html:
        modified_content["format"] = "org.matrix.custom.html"
        modified_content["formatted_body"] = _create_tool_aware_preview(
            source_content["formatted_body"],
            available,
        )

    return mxc_uri, file_info, modified_content


async def prepare_large_message(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    """Check if message is too large and prepare it if needed.

    This function:
    1. Checks the message size
    2. If too large, uploads the full text as MXC
    3. Replaces body with maximum-size preview
    4. Adds metadata for reconstruction

    Args:
        client: The Matrix client
        room_id: The room to send to
        content: The message content dictionary

    Returns:
        Original content (if small) or modified content with preview and MXC reference

    """
    is_edit = _is_edit_message(content)
    size_limit = EDIT_MESSAGE_LIMIT if is_edit else NORMAL_MESSAGE_LIMIT

    current_size = _calculate_event_size(content)
    if current_size <= size_limit:
        return content

    source_content = content["m.new_content"] if is_edit and "m.new_content" in content else content
    full_text = source_content["body"]
    formatted_body = source_content.get("formatted_body")
    has_tool_html = (
        source_content.get("format") == "org.matrix.custom.html"
        and isinstance(formatted_body, str)
        and _TOOL_BLOCK_RE.search(formatted_body) is not None
    )

    logger.info(f"Message too large ({current_size} bytes), uploading to MXC")

    mxc_uri, file_info, modified_content = await _build_file_content(
        client,
        room_id,
        source_content,
        full_text,
        has_tool_html,
        size_limit,
    )

    for key in PASSTHROUGH_CONTENT_KEYS:
        if key in source_content:
            modified_content[key] = source_content[key]

    if room_id and room_id in client.rooms and client.rooms[room_id].encrypted:
        modified_content["file"] = file_info
    else:
        modified_content["url"] = mxc_uri

    modified_content["io.mindroom.long_text"] = {
        "version": 1,
        "original_size": len(full_text),
        "preview_size": len(modified_content["body"]),
        "is_complete_text": True,
    }

    if "m.relates_to" in content:
        modified_content["m.relates_to"] = content["m.relates_to"]

    if is_edit and "m.new_content" in content:
        modified_content = {
            "msgtype": "m.text",
            "body": f"* {modified_content['body']}",
            "m.new_content": modified_content,
            "m.relates_to": content.get("m.relates_to", {}),
        }

    final_size = _calculate_event_size(modified_content)
    if final_size > 64000:
        logger.warning(f"Large message still exceeds 64KB after preparation ({final_size} bytes)")

    inner: dict[str, Any] = modified_content.get("m.new_content", modified_content)  # type: ignore[assignment]
    logger.info(
        f"Large message prepared: {len(full_text)} bytes -> {len(inner['body'])} preview + MXC attachment",
    )

    return modified_content
