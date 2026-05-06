"""Attachment toolkit for context-scoped file discovery and sending."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agno.tools import Toolkit

from mindroom.attachments import (
    AttachmentRecord,
    attachments_for_tool_payload,
    load_attachment,
    register_local_attachment,
    resolve_attachments,
)
from mindroom.custom_tools.attachment_helpers import room_access_allowed
from mindroom.matrix.client_delivery import send_file_message
from mindroom.tool_system.output_files import (
    ToolOutputFilePolicy,
    ensure_output_path_schema_optional,
    validate_output_path,
    validate_output_path_syntax,
    write_bytes_to_output_path,
)
from mindroom.tool_system.runtime_context import (
    append_tool_runtime_attachment_id,
    attachment_id_available_in_tool_runtime_context,
    get_tool_runtime_context,
    list_tool_runtime_attachment_ids,
)
from mindroom.tool_system.sandbox_proxy import (
    attachment_save_uses_worker,
    inline_attachment_byte_limit,
    save_attachment_to_worker,
)

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


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
    attachment, error = _resolve_context_attachment_record(context, attachment_id)
    if error is not None or attachment is None:
        return None, error
    return attachment.local_path, None


def _resolve_context_attachment_record(
    context: ToolRuntimeContext,
    attachment_id: str,
) -> tuple[AttachmentRecord | None, str | None]:
    """Resolve a context attachment ID to a readable attachment record."""
    if context.storage_path is None:
        return None, "Attachment storage path is unavailable in this runtime path."
    if not attachment_id_available_in_tool_runtime_context(context, attachment_id):
        return None, f"Attachment ID is not available in this context: {attachment_id}"

    attachment = load_attachment(context.storage_path, attachment_id)
    if attachment is None:
        return None, f"Attachment metadata not found: {attachment_id}"
    if not attachment.local_path.is_file():
        return None, f"Attachment file is missing on disk: {attachment_id}"
    return attachment, None


def _attachment_bytes_for_save(
    attachment: AttachmentRecord,
    *,
    byte_limit: int,
    limit_label: str,
) -> tuple[bytes | None, str | None]:
    """Read attachment bytes after enforcing the selected destination cap."""
    try:
        size_bytes = attachment.local_path.stat().st_size
    except OSError:
        return None, f"Attachment file is missing on disk: {attachment.attachment_id}"
    if size_bytes > byte_limit:
        return (
            None,
            f"Attachment {attachment.attachment_id} exceeds {limit_label} size limit "
            f"({size_bytes} bytes > {byte_limit} bytes).",
        )
    try:
        payload = attachment.local_path.read_bytes()
    except OSError:
        return None, f"Attachment file is missing on disk: {attachment.attachment_id}"
    return payload, None


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
        thread_id=context.resolved_thread_id,
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


def resolve_send_attachments(
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


async def send_attachment_paths(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    thread_id: str | None,
    attachment_paths: list[Path],
) -> tuple[list[str], str | None]:
    """Upload local attachment paths to Matrix, preserving order."""
    attachment_event_ids: list[str] = []
    assert context.conversation_cache is not None
    latest_thread_event_id = await context.conversation_cache.get_latest_thread_event_id_if_needed(
        room_id,
        thread_id,
        caller_label="attachment_tool_send",
    )
    for attachment_path in attachment_paths:
        attachment_event_id = await send_file_message(
            context.client,
            room_id,
            attachment_path,
            config=context.config,
            thread_id=thread_id,
            latest_thread_event_id=latest_thread_event_id,
            conversation_cache=context.conversation_cache,
        )
        if attachment_event_id is None:
            return attachment_event_ids, f"Failed to send attachment: {attachment_path}"
        attachment_event_ids.append(attachment_event_id)
        latest_thread_event_id = attachment_event_id
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
        resolve_send_attachments(
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

    attachment_event_ids, send_error = await send_attachment_paths(
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
        return effective_room_id, context.resolved_thread_id, None
    return effective_room_id, None, None


class AttachmentTools(Toolkit):
    """Toolkit for reading and sending context-scoped attachments."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        worker_tools_override: list[str] | None = None,
        tool_output_workspace_root: Path | None = None,
    ) -> None:
        self._runtime_paths = runtime_paths
        self._worker_target = worker_target
        self._worker_tools_override = worker_tools_override
        self._tool_output_workspace_root = tool_output_workspace_root
        super().__init__(
            name="attachments",
            tools=[
                self.list_attachments,
                self.get_attachment,
                self.register_attachment,
            ],
        )
        self._describe_get_attachment_schema()

    def _describe_get_attachment_schema(self) -> None:
        """Attach explicit model-facing descriptions for bespoke attachment args."""
        function = self.async_functions.get("get_attachment")
        if function is None:
            return
        ensure_output_path_schema_optional(function)
        parameters = dict(function.parameters)
        properties = dict(parameters.get("properties") or {})
        attachment_id_schema = dict(properties.get("attachment_id") or {})
        attachment_id_schema["description"] = "Context-scoped attachment ID returned by list_attachments."
        properties["attachment_id"] = attachment_id_schema
        parameters["properties"] = properties
        function.parameters = parameters

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

    async def get_attachment(  # noqa: PLR0911
        self,
        attachment_id: str,
        mindroom_output_path: str | None = None,
    ) -> str:
        """Return one context attachment record, or save its bytes to a workspace path."""
        context = get_tool_runtime_context()
        if context is None:
            return _attachment_tool_payload(
                "error",
                message="Tool runtime context is unavailable in this runtime path.",
            )
        if not isinstance(attachment_id, str) or not attachment_id.strip():
            return _attachment_tool_payload("error", message="attachment_id must be a non-empty string.")

        requested_attachment_id = attachment_id.strip()
        output_path, output_path_error = self._resolve_output_path_argument(
            mindroom_output_path=mindroom_output_path,
        )
        if output_path_error is not None:
            return _attachment_tool_payload("error", attachment_id=requested_attachment_id, message=output_path_error)
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
        if output_path is not None:
            return await self._save_attachment_to_output_path(
                context,
                requested_attachment_id=requested_attachment_ids[0],
                output_path=output_path,
            )

        return _attachment_tool_payload(
            "ok",
            attachment_id=requested_attachment_ids[0],
            attachment=attachments[0],
        )

    def _resolve_output_path_argument(
        self,
        *,
        mindroom_output_path: str | None,
    ) -> tuple[str | None, str | None]:
        """Resolve the model-facing output-path argument."""
        if mindroom_output_path is None:
            return None, None
        if not isinstance(mindroom_output_path, str):
            return None, "mindroom_output_path must be a workspace-relative string path."
        return mindroom_output_path, None

    def _save_destination(
        self,
        context: ToolRuntimeContext,
    ) -> tuple[bool, ToolOutputFilePolicy | None]:
        """Resolve whether this save lands on a worker and the local validation policy."""
        runtime_paths = self._runtime_paths or context.runtime_paths
        use_worker = attachment_save_uses_worker(
            runtime_paths=runtime_paths,
            worker_target=self._worker_target,
            worker_tools_override=self._worker_tools_override,
        )
        local_policy = (
            ToolOutputFilePolicy.from_runtime(self._tool_output_workspace_root, runtime_paths)
            if self._tool_output_workspace_root is not None
            else None
        )
        return use_worker, local_policy

    def _validate_output_path_before_save(
        self,
        *,
        output_path: str,
        use_worker: bool,
        local_policy: ToolOutputFilePolicy | None,
    ) -> str | None:
        """Validate the requested output path before reading attachment bytes."""
        if use_worker:
            return validate_output_path_syntax(output_path)
        if local_policy is not None:
            return validate_output_path(local_policy, output_path)
        return "mindroom_output_path requires an agent workspace in this runtime path."

    async def _save_attachment_to_output_path(  # noqa: C901, PLR0911, PLR0912
        self,
        context: ToolRuntimeContext,
        *,
        requested_attachment_id: str,
        output_path: str,
    ) -> str:
        use_worker, local_policy = self._save_destination(context)
        path_error = self._validate_output_path_before_save(
            output_path=output_path,
            use_worker=use_worker,
            local_policy=local_policy,
        )
        if path_error is not None:
            return _attachment_tool_payload("error", attachment_id=requested_attachment_id, message=path_error)

        attachment, resolve_error = _resolve_context_attachment_record(context, requested_attachment_id)
        if resolve_error is not None or attachment is None:
            return _attachment_tool_payload("error", attachment_id=requested_attachment_id, message=resolve_error)

        runtime_paths = self._runtime_paths or context.runtime_paths
        if use_worker:
            byte_limit = inline_attachment_byte_limit(runtime_paths)
            limit_label = "inline worker-transfer"
        elif local_policy is not None:
            byte_limit = local_policy.max_bytes
            limit_label = "tool output redirect"
        else:
            return _attachment_tool_payload(
                "error",
                attachment_id=requested_attachment_id,
                message="mindroom_output_path requires an agent workspace in this runtime path.",
            )
        payload_bytes, read_error = _attachment_bytes_for_save(
            attachment,
            byte_limit=byte_limit,
            limit_label=limit_label,
        )
        if read_error is not None or payload_bytes is None:
            return _attachment_tool_payload("error", attachment_id=requested_attachment_id, message=read_error)

        sha256 = hashlib.sha256(payload_bytes).hexdigest()
        attachment_payload = attachments_for_tool_payload([attachment])[0]
        attachment_payload.pop("local_path", None)

        if use_worker:
            try:
                worker_receipt = await asyncio.to_thread(
                    save_attachment_to_worker,
                    runtime_paths=runtime_paths,
                    worker_target=self._worker_target,
                    worker_tools_override=self._worker_tools_override,
                    attachment_id=requested_attachment_id,
                    mindroom_output_path=output_path,
                    payload_bytes=payload_bytes,
                    mime_type=attachment.mime_type,
                    filename=attachment.filename,
                )
            except Exception as exc:
                return _attachment_tool_payload("error", attachment_id=requested_attachment_id, message=str(exc))
            if worker_receipt is None:
                return _attachment_tool_payload(
                    "error",
                    attachment_id=requested_attachment_id,
                    message="Worker output workspace is unavailable for this attachment save.",
                )
            attachment_payload.update(
                {
                    "save_path": worker_receipt.worker_path,
                    "size_bytes": worker_receipt.size_bytes,
                    "sha256": worker_receipt.sha256,
                },
            )
            return _attachment_tool_payload(
                "ok",
                attachment_id=requested_attachment_id,
                attachment=attachment_payload,
                mindroom_tool_output={
                    "status": "saved_to_file",
                    "path": worker_receipt.worker_path,
                    "bytes": worker_receipt.size_bytes,
                    "format": "binary",
                    "sha256": worker_receipt.sha256,
                },
            )

        if local_policy is None:
            return _attachment_tool_payload(
                "error",
                attachment_id=requested_attachment_id,
                message="mindroom_output_path requires an agent workspace in this runtime path.",
            )
        write_result = write_bytes_to_output_path(local_policy, output_path, payload_bytes, file_mode=0o600)
        if isinstance(write_result, str):
            return _attachment_tool_payload("error", attachment_id=requested_attachment_id, message=write_result)

        output_receipt = write_result.receipt["mindroom_tool_output"]
        if not isinstance(output_receipt, Mapping):
            return _attachment_tool_payload(
                "error",
                attachment_id=requested_attachment_id,
                message="Failed to build attachment save receipt.",
            )
        output_receipt_map = cast("Mapping[str, object]", output_receipt)
        receipt_path = output_receipt_map.get("path")
        if not isinstance(receipt_path, str):
            return _attachment_tool_payload(
                "error",
                attachment_id=requested_attachment_id,
                message="Failed to build attachment save receipt.",
            )
        attachment_payload.update(
            {
                "save_path": receipt_path,
                "size_bytes": write_result.byte_count,
                "sha256": sha256,
            },
        )
        return _attachment_tool_payload(
            "ok",
            attachment_id=requested_attachment_id,
            attachment=attachment_payload,
            mindroom_tool_output={
                **output_receipt,
                "sha256": sha256,
            },
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
