"""Attachment toolkit for context-scoped file discovery and sending."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.attachments import (
    AttachmentRecord,
    attachments_for_tool_payload,
    load_attachment,
    register_local_attachment,
    resolve_attachments,
)
from mindroom.custom_tools.attachment_helpers import (
    room_access_allowed,
)
from mindroom.matrix.client import send_file_message
from mindroom.tool_system.runtime_context import (
    append_tool_runtime_attachment_id,
    attachment_id_available_in_tool_runtime_context,
    get_tool_runtime_context,
    list_tool_runtime_attachment_ids,
)

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


@dataclass(frozen=True)
class _AttachmentSendResult:
    """Result payload for internal attachment send operations."""

    room_id: str
    thread_id: str | None
    attachment_event_ids: list[str]
    resolved_attachment_ids: list[str]
    newly_registered_attachment_ids: list[str]


def _attachment_tool_payload(status: str, **kwargs: object) -> str:
    """Return a structured payload for the attachments tool."""
    payload: dict[str, object] = {
        "status": status,
        "tool": "attachments",
    }
    payload.update(kwargs)
    return json.dumps(payload, sort_keys=True)


def _get_attachment_listing(
    context: ToolRuntimeContext,
    target: str | None,
) -> tuple[list[str], list[dict[str, object]], list[str], str | None]:
    """List requested context attachments and report missing metadata records."""
    if context.storage_path is None:
        return [], [], [], "Attachment storage path is unavailable in this runtime path."

    requested_attachment_ids = list_tool_runtime_attachment_ids(context)
    if target and target.strip():
        target_attachment_id = target.strip()
        if not attachment_id_available_in_tool_runtime_context(context, target_attachment_id):
            return [], [], [], f"Attachment ID is not available in this context: {target_attachment_id}"
        requested_attachment_ids = [target_attachment_id]

    attachment_records = resolve_attachments(context.storage_path, requested_attachment_ids)
    resolved_attachment_ids = [record.attachment_id for record in attachment_records]
    missing_attachment_ids = [
        attachment_id for attachment_id in requested_attachment_ids if attachment_id not in resolved_attachment_ids
    ]
    return (
        requested_attachment_ids,
        attachments_for_tool_payload(attachment_records),
        missing_attachment_ids,
        None,
    )


def _resolve_context_attachment_path(
    context: ToolRuntimeContext,
    attachment_id: str,
) -> tuple[Path | None, str | None]:
    """Resolve a context attachment ID to a local file path."""
    if context.storage_path is None:
        return None, "Attachment storage path is unavailable in this runtime path."
    if not attachment_id_available_in_tool_runtime_context(context, attachment_id):
        return None, f"Attachment ID is not available in this context: {attachment_id}"

    attachment = load_attachment(context.storage_path, attachment_id)
    if attachment is None:
        return None, f"Attachment metadata not found: {attachment_id}"
    if not attachment.local_path.is_file():
        return None, f"Attachment file is missing on disk: {attachment_id}"
    return attachment.local_path, None


def _resolve_attachment_ids(
    context: ToolRuntimeContext,
    attachment_ids: list[str],
) -> tuple[list[Path], list[str], str | None]:
    """Resolve context attachment IDs into local files."""
    if not attachment_ids:
        return [], [], None

    resolved_paths: list[Path] = []
    resolved_attachment_ids: list[str] = []
    for attachment_id in attachment_ids:
        if not attachment_id.startswith("att_"):
            return [], [], "attachment_ids entries must be context attachment IDs (att_*)."

        attachment_path, error = _resolve_context_attachment_path(context, attachment_id)
        if error is not None:
            return [], [], error
        if attachment_path is None:
            continue

        resolved_paths.append(attachment_path)
        resolved_attachment_ids.append(attachment_id)
    return resolved_paths, resolved_attachment_ids, None


def _register_attachment_file_path(
    context: ToolRuntimeContext,
    file_path: str,
) -> tuple[AttachmentRecord | None, str | None]:
    """Register a local file path in the current tool context."""
    if context.storage_path is None:
        return None, "Attachment storage path is unavailable in this runtime path."

    resolved_path = Path(file_path).expanduser().resolve()
    attachment_record = register_local_attachment(
        context.storage_path,
        resolved_path,
        kind="file",
        room_id=context.room_id,
        thread_id=context.thread_id,
        sender=context.requester_id,
    )
    if attachment_record is None:
        return None, f"Failed to register attachment file: {resolved_path}"

    append_tool_runtime_attachment_id(attachment_record.attachment_id)
    return attachment_record, None


def _resolve_attachment_file_paths(
    context: ToolRuntimeContext,
    attachment_file_paths: list[str],
) -> tuple[list[Path], list[str], str | None]:
    """Register file paths and return local paths plus generated attachment IDs."""
    if not attachment_file_paths:
        return [], [], None

    resolved_paths: list[Path] = []
    newly_registered_attachment_ids: list[str] = []
    for attachment_file_path in attachment_file_paths:
        attachment_record, register_error = _register_attachment_file_path(context, attachment_file_path)
        if register_error is not None:
            return [], [], register_error
        if attachment_record is None:
            continue
        resolved_paths.append(attachment_record.local_path)
        newly_registered_attachment_ids.append(attachment_record.attachment_id)

    return resolved_paths, newly_registered_attachment_ids, None


def _resolve_send_attachments(
    context: ToolRuntimeContext,
    *,
    attachment_ids: list[str],
    attachment_file_paths: list[str],
) -> tuple[list[Path], list[str], list[str], str | None]:
    """Resolve context IDs and/or local file paths to sendable attachment paths."""
    attachment_paths, resolved_attachment_ids, attachment_error = _resolve_attachment_ids(
        context,
        attachment_ids,
    )
    if attachment_error is not None:
        return [], [], [], attachment_error
    file_paths, newly_registered_attachment_ids, file_path_error = _resolve_attachment_file_paths(
        context,
        attachment_file_paths,
    )
    if file_path_error is not None:
        return [], [], [], file_path_error
    attachment_paths.extend(file_paths)
    resolved_attachment_ids.extend(newly_registered_attachment_ids)
    if not attachment_paths:
        return [], [], [], "At least one of attachment_ids or attachment_file_paths must be provided."
    return attachment_paths, resolved_attachment_ids, newly_registered_attachment_ids, None


async def _send_attachment_paths(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    thread_id: str | None,
    attachment_paths: list[Path],
) -> tuple[list[str], str | None]:
    """Upload local attachment paths to Matrix, preserving order."""
    attachment_event_ids: list[str] = []
    for attachment_path in attachment_paths:
        attachment_event_id = await send_file_message(
            context.client,
            room_id,
            attachment_path,
            thread_id=thread_id,
        )
        if attachment_event_id is None:
            return attachment_event_ids, f"Failed to send attachment: {attachment_path}"
        attachment_event_ids.append(attachment_event_id)
    return attachment_event_ids, None


async def send_context_attachments(
    context: ToolRuntimeContext,
    *,
    attachment_ids: list[str],
    attachment_file_paths: list[str],
    room_id: str | None = None,
    thread_id: str | None = None,
    require_joined_room: bool = True,
    inherit_context_thread: bool = True,
) -> tuple[_AttachmentSendResult | None, str | None]:
    """Resolve and send context-scoped attachments to Matrix."""
    attachment_paths, resolved_attachment_ids, newly_registered_attachment_ids, resolve_error = (
        _resolve_send_attachments(
            context,
            attachment_ids=attachment_ids,
            attachment_file_paths=attachment_file_paths,
        )
    )
    if resolve_error is not None:
        return None, resolve_error

    effective_room_id, effective_thread_id, destination_error = _resolve_send_target(
        context,
        room_id=room_id,
        thread_id=thread_id,
        require_joined_room=require_joined_room,
        inherit_context_thread=inherit_context_thread,
    )
    if destination_error is not None:
        return (
            _AttachmentSendResult(
                room_id=effective_room_id,
                thread_id=effective_thread_id,
                attachment_event_ids=[],
                resolved_attachment_ids=resolved_attachment_ids,
                newly_registered_attachment_ids=newly_registered_attachment_ids,
            ),
            destination_error,
        )

    attachment_event_ids, send_error = await _send_attachment_paths(
        context,
        room_id=effective_room_id,
        thread_id=effective_thread_id,
        attachment_paths=attachment_paths,
    )
    result = _AttachmentSendResult(
        room_id=effective_room_id,
        thread_id=effective_thread_id,
        attachment_event_ids=attachment_event_ids,
        resolved_attachment_ids=resolved_attachment_ids,
        newly_registered_attachment_ids=newly_registered_attachment_ids,
    )
    if send_error is not None:
        return result, send_error
    return result, None


def _resolve_send_target(
    context: ToolRuntimeContext,
    *,
    room_id: str | None,
    thread_id: str | None,
    require_joined_room: bool = True,
    inherit_context_thread: bool = True,
) -> tuple[str, str | None, str | None]:
    """Resolve room/thread destination and validate room access for sending."""
    effective_room_id = room_id or context.room_id
    if not room_access_allowed(context, effective_room_id):
        return effective_room_id, None, "Not authorized to access the target room."
    if require_joined_room and effective_room_id not in context.client.rooms:
        return effective_room_id, None, f"Cannot send to room {effective_room_id}: bot has not joined this room."
    if thread_id is not None:
        return effective_room_id, thread_id, None
    if inherit_context_thread and effective_room_id == context.room_id:
        return effective_room_id, context.thread_id, None
    return effective_room_id, None, None


class AttachmentTools(Toolkit):
    """Toolkit for reading and sending context-scoped attachments."""

    def __init__(self) -> None:
        super().__init__(
            name="attachments",
            tools=[
                self.list_attachments,
                self.get_attachment,
                self.register_attachment,
            ],
        )

    async def list_attachments(self, target: str | None = None) -> str:
        """List attachment metadata for current tool context."""
        context = get_tool_runtime_context()
        if context is None:
            return _attachment_tool_payload(
                "error",
                message="Tool runtime context is unavailable in this runtime path.",
            )

        requested_attachment_ids, attachments, missing_attachment_ids, error = _get_attachment_listing(context, target)
        if error is not None:
            return _attachment_tool_payload("error", message=error)

        return _attachment_tool_payload(
            "ok",
            attachment_ids=requested_attachment_ids,
            attachments=attachments,
            missing_attachment_ids=missing_attachment_ids,
        )

    async def get_attachment(self, attachment_id: str) -> str:
        """Return one context attachment record, including local file path."""
        context = get_tool_runtime_context()
        if context is None:
            return _attachment_tool_payload(
                "error",
                message="Tool runtime context is unavailable in this runtime path.",
            )
        if not isinstance(attachment_id, str) or not attachment_id.strip():
            return _attachment_tool_payload("error", message="attachment_id must be a non-empty string.")

        requested_attachment_id = attachment_id.strip()
        requested_attachment_ids, attachments, missing_attachment_ids, error = _get_attachment_listing(
            context,
            requested_attachment_id,
        )
        if error is not None:
            return _attachment_tool_payload("error", message=error)
        if missing_attachment_ids:
            return _attachment_tool_payload(
                "error",
                attachment_id=requested_attachment_id,
                message=f"Attachment metadata not found: {requested_attachment_id}",
            )
        if not attachments:
            return _attachment_tool_payload(
                "error",
                attachment_id=requested_attachment_id,
                message=f"Attachment not found in context: {requested_attachment_id}",
            )

        return _attachment_tool_payload(
            "ok",
            attachment_id=requested_attachment_ids[0],
            attachment=attachments[0],
        )

    async def register_attachment(self, file_path: str) -> str:
        """Register a local file as a context attachment ID."""
        context = get_tool_runtime_context()
        if context is None:
            return _attachment_tool_payload(
                "error",
                message="Tool runtime context is unavailable in this runtime path.",
            )
        if not isinstance(file_path, str) or not file_path.strip():
            return _attachment_tool_payload("error", message="file_path must be a non-empty string.")

        attachment_record, register_error = _register_attachment_file_path(context, file_path.strip())
        if register_error is not None or attachment_record is None:
            return _attachment_tool_payload(
                "error",
                message=register_error or "Failed to register attachment file.",
            )

        return _attachment_tool_payload(
            "ok",
            attachment_id=attachment_record.attachment_id,
            attachment=attachments_for_tool_payload([attachment_record])[0],
        )
