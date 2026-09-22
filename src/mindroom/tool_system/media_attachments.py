"""Retain tool-produced images as authorized attachments without publishing them."""

from __future__ import annotations

import json
from uuid import uuid4

from agno.tools.function import ToolResult

from mindroom.attachments import register_image_bytes_attachment
from mindroom.tool_system.runtime_context import append_tool_runtime_attachment_id, get_tool_runtime_context


def _retain_tool_media(result: object) -> object:  # noqa: PLR0911
    """Keep delivered bytes for reuse; never resolve paths supplied by a worker."""
    context = get_tool_runtime_context()
    if not isinstance(result, ToolResult) or not result.images or context is None or context.storage_path is None:
        return result
    if len(result.images) != 1 or not isinstance(result.content, str):
        return result
    try:
        metadata = json.loads(result.content)
    except (ValueError, TypeError):
        return result
    if not isinstance(metadata, dict) or metadata.get("view_status") != "ready":
        return result
    if metadata.get("attachment_id"):
        return result
    image = result.images[0]
    if not isinstance(image.content, bytes) or image.mime_type not in {"image/png", "image/jpeg"}:
        return result
    attachment_id = f"att_{uuid4().hex[:16]}"
    extension = ".png" if image.mime_type == "image/png" else ".jpg"
    record = register_image_bytes_attachment(
        context.storage_path,
        image.content,
        attachment_id=attachment_id,
        filename=f"viewed-image{extension}",
        mime_type=image.mime_type,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id,
        sender=context.requester_id,
    )
    if record is None:
        metadata["attachment_warning"] = "Image is viewable, but a reusable attachment could not be retained."
    else:
        append_tool_runtime_attachment_id(record.attachment_id)
        metadata["attachment_id"] = record.attachment_id
        metadata["attachment_is_viewed_copy"] = True
    return ToolResult(content=json.dumps(metadata, sort_keys=True), images=result.images)


def finalize_tool_media(result: object) -> object:
    """Retain artifacts before reporting a known model-adapter limitation."""
    context = get_tool_runtime_context()
    if context is None:
        return result
    from mindroom.provider_media_fallback import guard_tool_image_result  # noqa: PLC0415

    return guard_tool_image_result(_retain_tool_media(result), context=context)
