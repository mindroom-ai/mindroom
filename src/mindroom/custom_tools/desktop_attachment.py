"""Turn-scoped attachment handles for desktop and real-profile browser screenshots."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.matrix.runtime_media import RuntimeEncryptedMediaAttachment
from mindroom.tool_system.runtime_context import ToolRuntimeContext, register_tool_runtime_media_attachment

if TYPE_CHECKING:
    from mindroom.desktop.protocol import EncryptedDesktopMedia


def register_runtime_screenshot_attachment(
    context: ToolRuntimeContext,
    media: EncryptedDesktopMedia,
    *,
    filename_prefix: str,
) -> RuntimeEncryptedMediaAttachment:
    """Expose encrypted screenshot media as an attachment for the active turn only."""
    attachment_id = f"att_{uuid4().hex[:16]}"
    extension = "jpg" if media.mime_type == "image/jpeg" else "png"
    attachment = RuntimeEncryptedMediaAttachment(
        attachment_id=attachment_id,
        filename=f"{filename_prefix}-{attachment_id[4:]}.{extension}",
        url=media.url,
        key=media.key,
        iv=media.iv,
        sha256=media.sha256,
        mime_type=media.mime_type,
        size=media.size,
    )
    register_tool_runtime_media_attachment(context, attachment)
    return attachment


def screenshot_attachment_result_fields(attachment: RuntimeEncryptedMediaAttachment) -> dict[str, object]:
    """Return concise model guidance for sending an ephemeral screenshot handle."""
    return {
        "attachment_id": attachment.attachment_id,
        "attachment": attachment.tool_payload(),
        "attachment_lifetime": "current_turn",
        "attachment_usage": (
            "Send this screenshot in the current turn with matrix_message attachment_ids; "
            "the handle expires when the turn ends."
        ),
    }
