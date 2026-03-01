"""Attachment toolkit for context-scoped file discovery and sending."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from agno.tools import Toolkit

from mindroom.attachments import attachments_for_tool_payload, load_attachment, resolve_attachments
from mindroom.attachments_context import get_attachment_tool_context
from mindroom.matrix.client import send_file_message

if TYPE_CHECKING:
    from mindroom.attachments_context import AttachmentToolContext


class ResolvedAttachmentReference(NamedTuple):
    """Result of resolving an attachment reference string."""

    path: Path | None
    attachment_id: str | None
    error: str | None


def attachment_tool_payload(status: str, **kwargs: object) -> str:
    """Return a structured payload for the attachments tool."""
    payload: dict[str, object] = {
        "status": status,
        "tool": "attachments",
    }
    payload.update(kwargs)
    return json.dumps(payload, sort_keys=True)


def _is_within_directory(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _resolve_context_attachment_path(
    context: AttachmentToolContext,
    attachment_id: str,
) -> tuple[Path | None, str | None]:
    if attachment_id not in context.attachment_ids:
        return None, f"Attachment ID is not available in this context: {attachment_id}"

    attachment = load_attachment(context.storage_path, attachment_id)
    if attachment is None:
        return None, f"Attachment metadata not found: {attachment_id}"
    if not attachment.local_path.is_file():
        return None, f"Attachment file is missing on disk: {attachment_id}"
    return attachment.local_path, None


def _resolve_attachment_reference(
    context: AttachmentToolContext,
    raw_reference: object,
    *,
    allow_local_paths: bool,
) -> ResolvedAttachmentReference:
    if not isinstance(raw_reference, str):
        return ResolvedAttachmentReference(None, None, "attachments entries must be strings.")

    reference = raw_reference.strip()
    if not reference:
        return ResolvedAttachmentReference(None, None, None)

    if reference.startswith("att_"):
        attachment_path, error = _resolve_context_attachment_path(context, reference)
        if error is not None:
            return ResolvedAttachmentReference(None, None, error)
        return ResolvedAttachmentReference(attachment_path, reference, None)

    if not allow_local_paths:
        error = "Local file paths are disabled. Use attachment IDs or set allow_local_paths=true."
    else:
        storage_path = context.storage_path.expanduser().resolve()
        path = Path(reference).expanduser().resolve()
        if not _is_within_directory(path, storage_path):
            error = f"Local file paths must be under storage path: {storage_path}"
        elif not path.is_file():
            error = f"Attachment path is not a file: {reference}"
        else:
            return ResolvedAttachmentReference(path, None, None)

    return ResolvedAttachmentReference(None, None, error)


def resolve_attachment_references(
    context: AttachmentToolContext,
    attachments: list[str] | None,
    *,
    allow_local_paths: bool = False,
) -> tuple[list[Path], list[str], str | None]:
    """Resolve context attachment IDs or file paths into local files."""
    if not attachments:
        return [], [], None

    resolved_paths: list[Path] = []
    resolved_attachment_ids: list[str] = []
    for raw_reference in attachments:
        resolved = _resolve_attachment_reference(
            context,
            raw_reference,
            allow_local_paths=allow_local_paths,
        )
        if resolved.error is not None:
            return [], [], resolved.error
        if resolved.path is None:
            continue

        resolved_paths.append(resolved.path)
        if resolved.attachment_id is not None:
            resolved_attachment_ids.append(resolved.attachment_id)
    return resolved_paths, resolved_attachment_ids, None


def get_attachment_listing(
    context: AttachmentToolContext,
    target: str | None,
) -> tuple[list[str], list[dict[str, object]], list[str], str | None]:
    """List requested context attachments and report missing metadata records."""
    requested_attachment_ids = list(context.attachment_ids)
    if target and target.strip():
        target_attachment_id = target.strip()
        if target_attachment_id not in context.attachment_ids:
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


async def send_attachment_paths(
    context: AttachmentToolContext,
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


class AttachmentTools(Toolkit):
    """Toolkit for reading and sending context-scoped attachments."""

    def __init__(self) -> None:
        super().__init__(
            name="attachments",
            tools=[
                self.list_attachments,
                self.send_attachments,
            ],
        )

    async def list_attachments(self, target: str | None = None) -> str:
        """List attachment metadata for current tool context."""
        context = get_attachment_tool_context()
        if context is None:
            return attachment_tool_payload(
                "error",
                message="Attachment tool context is unavailable in this runtime path.",
            )

        requested_attachment_ids, attachments, missing_attachment_ids, error = get_attachment_listing(context, target)
        if error is not None:
            return attachment_tool_payload("error", message=error)

        return attachment_tool_payload(
            "ok",
            attachment_ids=requested_attachment_ids,
            attachments=attachments,
            missing_attachment_ids=missing_attachment_ids,
        )

    async def send_attachments(
        self,
        attachments: list[str] | None = None,
        room_id: str | None = None,
        thread_id: str | None = None,
        allow_local_paths: bool = False,
    ) -> str:
        """Send attachment IDs or storage-scoped local file paths to a Matrix room/thread."""
        context = get_attachment_tool_context()
        if context is None:
            return attachment_tool_payload(
                "error",
                message="Attachment tool context is unavailable in this runtime path.",
            )

        attachment_paths, resolved_attachment_ids, attachment_error = resolve_attachment_references(
            context,
            attachments,
            allow_local_paths=allow_local_paths,
        )
        if attachment_error is not None:
            return attachment_tool_payload("error", message=attachment_error)
        if not attachment_paths:
            return attachment_tool_payload("error", message="attachments cannot be empty.")

        effective_room_id = room_id or context.room_id
        effective_thread_id = context.thread_id if thread_id is None else thread_id
        attachment_event_ids, send_error = await send_attachment_paths(
            context,
            room_id=effective_room_id,
            thread_id=effective_thread_id,
            attachment_paths=attachment_paths,
        )
        if send_error is not None:
            return attachment_tool_payload(
                "error",
                room_id=effective_room_id,
                thread_id=effective_thread_id,
                attachment_event_ids=attachment_event_ids,
                resolved_attachment_ids=resolved_attachment_ids,
                message=send_error,
            )

        return attachment_tool_payload(
            "ok",
            room_id=effective_room_id,
            thread_id=effective_thread_id,
            event_id=attachment_event_ids[-1] if attachment_event_ids else None,
            attachment_event_ids=attachment_event_ids,
            resolved_attachment_ids=resolved_attachment_ids,
        )
