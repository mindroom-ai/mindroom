"""Shared helpers for attachment-aware tool actions."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.attachments import AttachmentRecord, load_attachment, register_local_attachment
from mindroom.authorization import is_authorized_sender
from mindroom.tool_runtime_context import append_tool_runtime_attachment_id

if TYPE_CHECKING:
    from mindroom.tool_runtime_context import ToolRuntimeContext


def normalize_str_list(values: list[str] | None, *, field_name: str) -> tuple[list[str], str | None]:
    """Validate and strip a list of string values, returning normalized list and optional error."""
    if values is None:
        return [], None

    normalized: list[str] = []
    for raw_value in values:
        if not isinstance(raw_value, str):
            return [], f"{field_name} entries must be strings."
        value = raw_value.strip()
        if value:
            normalized.append(value)
    return normalized, None


def room_access_allowed(context: ToolRuntimeContext, room_id: str) -> bool:
    """Return whether the requester may act in the given room."""
    if room_id == context.room_id:
        return True
    room_alias = room_id if room_id.startswith("#") else None
    return is_authorized_sender(
        context.requester_id,
        context.config,
        room_id,
        room_alias=room_alias,
    )


def resolve_context_attachment_path(
    context: ToolRuntimeContext,
    attachment_id: str,
) -> tuple[Path | None, str | None]:
    """Resolve a context attachment ID to a local file path."""
    if context.storage_path is None:
        return None, "Attachment storage path is unavailable in this runtime path."
    if attachment_id not in context.attachment_ids:
        return None, f"Attachment ID is not available in this context: {attachment_id}"

    attachment = load_attachment(context.storage_path, attachment_id)
    if attachment is None:
        return None, f"Attachment metadata not found: {attachment_id}"
    if not attachment.local_path.is_file():
        return None, f"Attachment file is missing on disk: {attachment_id}"
    return attachment.local_path, None


def resolve_attachment_ids(
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

        attachment_path, error = resolve_context_attachment_path(context, attachment_id)
        if error is not None:
            return [], [], error
        if attachment_path is None:
            continue

        resolved_paths.append(attachment_path)
        resolved_attachment_ids.append(attachment_id)
    return resolved_paths, resolved_attachment_ids, None


def register_attachment_file_path(
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


def resolve_attachment_file_paths(
    context: ToolRuntimeContext,
    attachment_file_paths: list[str],
) -> tuple[list[Path], list[str], str | None]:
    """Register file paths and return local paths plus generated attachment IDs."""
    if not attachment_file_paths:
        return [], [], None

    resolved_paths: list[Path] = []
    newly_registered_attachment_ids: list[str] = []
    for attachment_file_path in attachment_file_paths:
        attachment_record, register_error = register_attachment_file_path(context, attachment_file_path)
        if register_error is not None:
            return [], [], register_error
        if attachment_record is None:
            continue
        resolved_paths.append(attachment_record.local_path)
        newly_registered_attachment_ids.append(attachment_record.attachment_id)

    return resolved_paths, newly_registered_attachment_ids, None
