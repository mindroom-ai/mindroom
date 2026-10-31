"""Retain tool-produced images as authorized attachments without publishing them."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.attachments.storage import register_bytes_attachment
from mindroom.tool_system.runtime_context import append_tool_runtime_attachment_id, get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolValue


def _ready_receipt(result: ToolValue) -> dict[str, Any] | None:
    """Return the JSON receipt of a ready image view, or None when ``result`` is not one."""
    try:
        receipt = json.loads(result.content)
    except ValueError:
        return None
    return receipt if isinstance(receipt, dict) and receipt.get("view_status") == "ready" else None


def finalize_tool_media(result: ToolValue) -> ToolValue:
    """Keep a ready image view's delivered bytes as an attachment for reuse; never resolve paths supplied by a worker.

    The request pipeline marks the view unsupported for a model that cannot see images.
    """
    context = get_tool_runtime_context()
    if context is None or context.storage_path is None or len(result.images) != 1:
        return result
    metadata = _ready_receipt(result)
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
    return replace(result, content=json.dumps(metadata, sort_keys=True))
