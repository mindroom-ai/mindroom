"""Retain tool-produced images as authorized attachments without publishing them."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.attachments.storage import register_bytes_attachment
from mindroom.providers.registry import model_capabilities
from mindroom.tool_system.agno_toolkits import ToolResult
from mindroom.tool_system.runtime_context import append_tool_runtime_attachment_id, get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _ready_receipt(result: ToolResult) -> dict[str, Any] | None:
    """Return the JSON receipt of a ready image view, or None when ``result`` is not one."""
    if not isinstance(result.content, str):
        return None
    try:
        receipt = json.loads(result.content)
    except (ValueError, TypeError):
        return None
    return receipt if isinstance(receipt, dict) and receipt.get("view_status") == "ready" else None


def _retain_tool_media(result: object) -> object:
    """Keep delivered bytes for reuse; never resolve paths supplied by a worker."""
    context = get_tool_runtime_context()
    if not isinstance(result, ToolResult) or not result.images or context is None or context.storage_path is None:
        return result
    metadata = _ready_receipt(result) if len(result.images) == 1 else None
    if metadata is None or metadata.get("attachment_id"):
        return result
    image = result.images[0]
    if not isinstance(image.content, bytes) or image.mime_type not in {"image/png", "image/jpeg"}:
        return result
    attachment_id = f"att_{uuid4().hex[:16]}"
    extension = ".png" if image.mime_type == "image/png" else ".jpg"
    record = register_bytes_attachment(
        context.storage_path,
        image.content,
        kind="image",
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


def _active_model_omits_images(context: ToolRuntimeContext) -> bool:
    """Return whether the active model's route never sends images, as the route's ``omitted_media`` says."""
    config = context.current_config
    model_name = context.active_model_name
    if model_name is None or model_name not in config.models:
        return False
    return "image" in model_capabilities(config, context.runtime_paths, model_name).omitted_media


def _image_marked_unsupported(result: object, context: ToolRuntimeContext) -> object:
    """Turn a ready image view into an explicit unsupported result when the active model cannot see it."""
    if not isinstance(result, ToolResult) or not result.images or not _active_model_omits_images(context):
        return result
    receipt = _ready_receipt(result)
    if receipt is None:
        return result
    receipt["view_status"] = "unsupported"
    receipt["message"] = "The active model adapter does not support viewed image tool results."
    return result.model_copy(update={"content": json.dumps(receipt, sort_keys=True), "images": None})


def finalize_tool_media(result: object) -> object:
    """Retain artifacts before reporting a known model-adapter limitation."""
    context = get_tool_runtime_context()
    if context is None:
        return result
    return _image_marked_unsupported(_retain_tool_media(result), context)
