"""Attachment toolkit for context-scoped file discovery and sending."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from mindroom.attachments.storage import (
    AttachmentRecord,
    attachments_for_tool_payload,
    filter_attachments_for_context,
    load_attachment,
    register_local_attachment,
)
from mindroom.constants import SKIP_MENTIONS_KEY
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.file_access import AuthorizedFile, resolve_agent_file
from mindroom.llm.media import DOCUMENT_MIME_TYPES, Audio, File, Video
from mindroom.matrix.client_delivery import send_file_message, send_runtime_encrypted_media_message
from mindroom.matrix.media import resolve_image_mime_type
from mindroom.matrix.runtime_media import RuntimeEncryptedMediaAttachment
from mindroom.media_delivery import MAX_SOURCE_BYTES, image_result, media_error, view_agent_image
from mindroom.path_confinement import open_regular_file_within_root
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.media_attachments import finalize_tool_media
from mindroom.tool_system.output_files import (
    ToolOutputFilePolicy,
    saved_tool_output_receipt,
    validate_output_path,
    validate_output_path_syntax,
    with_optional_output_path,
    write_bytes_to_output_path,
)
from mindroom.tool_system.runtime_context import (
    append_tool_runtime_attachment_id,
    attachment_id_available_in_tool_runtime_context,
    get_tool_runtime_context,
    get_tool_runtime_media_attachment,
    list_tool_runtime_attachment_ids,
)
from mindroom.tool_system.sandbox_proxy import (
    inline_attachment_byte_limit,
    save_attachment_to_worker,
    view_file_from_worker,
)
from mindroom.tool_system.tool_function import ToolValue

if TYPE_CHECKING:
    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_LocalAttachmentKind = Literal["audio", "file", "image", "video"]
_ResolvedSendAttachment = AttachmentRecord | RuntimeEncryptedMediaAttachment
_WORKER_ATTACHMENT_USAGE = (
    "Not in your workspace. Copy it there with get_attachment(attachment_id, mindroom_output_path=...) "
    "before file, coding, python, or shell tools use it."
)


def _attachment_tool_payload(status: str, **kwargs: object) -> str:
    return custom_tool_payload("attachments", status, **kwargs)


def _infer_local_attachment_metadata(local_path: Path) -> tuple[_LocalAttachmentKind, str, str | None]:
    """Infer attachment metadata for local files registered by path."""
    mime_type, _encoding = mimetypes.guess_type(local_path.name)
    if mime_type is None:
        return "file", local_path.name, None
    media_type = mime_type.split("/", 1)[0].lower()
    if media_type in {"audio", "image", "video"}:
        return cast("_LocalAttachmentKind", media_type), local_path.name, mime_type
    return "file", local_path.name, mime_type


def _attachment_record_payload(record: AttachmentRecord, *, tools_use_worker: bool) -> dict[str, object]:
    """Render one retained attachment, replacing the runtime path with copy instructions for worker tools."""
    payload = attachments_for_tool_payload([record])[0]
    if tools_use_worker:
        # The path names the primary runtime's storage, which worker tools cannot open.
        del payload["local_path"]
        payload["usage"] = _WORKER_ATTACHMENT_USAGE
    return payload


def _get_attachment_listing(
    context: ToolRuntimeContext,
    target: str | None,
    *,
    tools_use_worker: bool,
) -> tuple[list[str], list[dict[str, object]], list[str], str | None]:
    """List requested context attachments and report missing metadata records."""
    requested_attachment_ids = list_tool_runtime_attachment_ids(context)
    if target and target.strip():
        target_attachment_id = target.strip()
        if not attachment_id_available_in_tool_runtime_context(context, target_attachment_id):
            return [], [], [], f"Attachment ID is not available in this context: {target_attachment_id}"
        requested_attachment_ids = [target_attachment_id]

    attachments: list[dict[str, object]] = []
    resolved_attachment_ids: list[str] = []
    for attachment_id in requested_attachment_ids:
        runtime_media = get_tool_runtime_media_attachment(context, attachment_id)
        if runtime_media is not None:
            attachments.append(runtime_media.tool_payload())
            resolved_attachment_ids.append(attachment_id)
            continue
        if context.storage_path is None:
            continue
        attachment_record = load_attachment(context.storage_path, attachment_id)
        if attachment_record is not None:
            attachments.append(_attachment_record_payload(attachment_record, tools_use_worker=tools_use_worker))
            resolved_attachment_ids.append(attachment_id)
    missing_attachment_ids = [
        attachment_id for attachment_id in requested_attachment_ids if attachment_id not in resolved_attachment_ids
    ]
    return (
        requested_attachment_ids,
        attachments,
        missing_attachment_ids,
        None,
    )


def resolve_context_attachment_path(
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
    if get_tool_runtime_media_attachment(context, attachment_id) is not None:
        return None, f"Ephemeral attachment {attachment_id} can be sent but has no local file."

    attachment = load_attachment(context.storage_path, attachment_id)
    if attachment is None:
        return None, f"Attachment metadata not found: {attachment_id}"
    if not attachment.local_path.is_file():
        return None, f"Attachment file is missing on disk: {attachment_id}"
    return attachment, None


def _read_attachment_bytes(
    attachment: AttachmentRecord,
    *,
    byte_limit: int,
    limit_label: str,
) -> tuple[bytes | None, str | None]:
    """Read attachment bytes with a bounded no-follow read and the selected destination cap."""
    try:
        with (
            open_regular_file_within_root(attachment.local_path.parent, attachment.local_path.name) as descriptor,
            os.fdopen(descriptor, "rb", closefd=False) as attachment_file,
        ):
            size_bytes = os.fstat(descriptor).st_size
            if size_bytes > byte_limit:
                return (
                    None,
                    f"Attachment {attachment.attachment_id} exceeds {limit_label} size limit "
                    f"({size_bytes} bytes > {byte_limit} bytes).",
                )
            payload = attachment_file.read(byte_limit + 1)
    except (OSError, ValueError):
        return None, f"Attachment file is missing on disk: {attachment.attachment_id}"
    if len(payload) > byte_limit:
        return None, f"Attachment {attachment.attachment_id} exceeds {limit_label} size limit ({byte_limit} bytes)."
    return payload, None


def _view_attachment(  # noqa: PLR0911
    context: ToolRuntimeContext,
    attachment_id: str,
    *,
    metadata: dict[str, object],
) -> ToolValue:
    """Read authorized attachment bytes and use shared preparation for every image view."""
    attachment, error = _resolve_context_attachment_record(context, attachment_id)
    if error is not None or attachment is None:
        return media_error(error or "Attachment is unavailable.", metadata=metadata)
    payload, error = _read_attachment_bytes(
        attachment,
        byte_limit=MAX_SOURCE_BYTES,
        limit_label="media viewing",
    )
    if error is not None or payload is None:
        return media_error(error or "Attachment is unavailable.", metadata=metadata)
    if not payload:
        return media_error("Attachment file is empty.", metadata=metadata)
    mime_type = resolve_image_mime_type(payload, attachment.mime_type).detected_mime_type
    if attachment.kind == "image" or mime_type is not None:
        allowed, _ = filter_attachments_for_context(
            [attachment],
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
        )
        if not allowed:
            return media_error("Attachment is outside the authorized conversation context.", metadata=metadata)
        return image_result(payload, metadata=metadata)
    content = json.dumps(metadata, sort_keys=True)
    mime_type = attachment.mime_type
    filename = attachment.filename or attachment.local_path.name
    media_format = Path(filename).suffix.lstrip(".").lower()
    if attachment.kind == "audio":
        return ToolValue(content=content, audios=(Audio(content=payload, mime_type=mime_type, format=media_format),))
    if attachment.kind == "video":
        return ToolValue(content=content, videos=(Video(content=payload, mime_type=mime_type, format=media_format),))
    if mime_type in DOCUMENT_MIME_TYPES:
        return ToolValue(
            content=content,
            files=(File(content=payload, mime_type=mime_type, filename=filename, format=media_format),),
        )
    return media_error(
        "This file type cannot be sent as model media. Use get_attachment without view to inspect or save it.",
        metadata=metadata,
    )


def _register_attachment_file_path(
    context: ToolRuntimeContext,
    file_path: str,
    *,
    workspace_root: Path | None,
    file_access: FileAccess,
) -> tuple[AttachmentRecord | None, str | None]:
    """Register a local file path in the current tool context."""
    if context.storage_path is None:
        return None, "Attachment storage path is unavailable in this runtime path."

    authorized, path_error = _resolve_attachment_file_path(
        file_path,
        workspace_root=workspace_root,
        file_access=file_access,
    )
    if path_error is not None or authorized is None:
        return None, path_error
    source_root = authorized.root
    resolved_path = authorized.root / authorized.relative
    kind, filename, mime_type = _infer_local_attachment_metadata(resolved_path)
    attachment_record = register_local_attachment(
        context.storage_path,
        resolved_path,
        kind=kind,
        source_root=source_root,
        filename=filename,
        mime_type=mime_type,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id,
        sender=context.requester_id,
    )
    if attachment_record is None:
        return None, f"Failed to register attachment file: {resolved_path}"

    append_tool_runtime_attachment_id(attachment_record.attachment_id)
    return attachment_record, None


def _resolve_attachment_file_path(
    file_path: str,
    *,
    workspace_root: Path | None,
    file_access: FileAccess,
) -> tuple[AuthorizedFile | None, str | None]:
    """Resolve one model-requested attachment file path under the agent's file access.

    The path is model-supplied and reaches an open in the primary process, so
    workspace access confines it to the workspace and fails closed without one;
    only already authorized ``att_*`` IDs then remain sendable. Registration
    copies the file below the returned root without following links.
    """
    try:
        authorized = resolve_agent_file(
            file_path,
            workspace_root=workspace_root,
            file_access=file_access,
            field_name="attachment file path",
        )
    except ValueError as exc:
        return None, str(exc)
    return authorized, None


def resolve_send_attachment(
    context: ToolRuntimeContext,
    reference: str,
    *,
    workspace_root: Path | None,
    file_access: FileAccess,
) -> tuple[_ResolvedSendAttachment, bool] | str:
    """Resolve one send reference to a sendable attachment and whether this call registered it, or an error.

    An ``att_*`` reference is a context attachment ID; any other reference is a file path to register,
    so a workspace file named ``att_*`` is sent as ``./att_*``.
    """
    if reference.startswith("att_"):
        runtime_media = get_tool_runtime_media_attachment(context, reference)
        if runtime_media is not None:
            return runtime_media, False
        record, error = _resolve_context_attachment_record(context, reference)
        newly_registered = False
    else:
        record, error = _register_attachment_file_path(
            context,
            reference,
            workspace_root=workspace_root,
            file_access=file_access,
        )
        newly_registered = True
    if error is not None:
        return error
    assert record is not None
    return record, newly_registered


async def send_resolved_attachments(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    thread_id: str | None,
    attachments: list[_ResolvedSendAttachment],
    known_latest_thread_event_id: str | None = None,
) -> tuple[list[str], str | None]:
    """Send retained files or already-encrypted Matrix media while preserving order.

    ``known_latest_thread_event_id`` is for a caller that already sent into this
    thread in this same execution. Each attachment after the first chains from
    the send response of the one before it; this is the same fact for the first,
    which the projection cannot supply until the earlier send echoes back.

    Each event asks receivers to ignore its mentions, because its body is a file
    name rather than an address, so a name like ``@agent.txt`` wakes no entity.
    """
    attachment_event_ids: list[str] = []
    latest_thread_event_id = await context.conversation_reader.latest_thread_event_id(
        room_id=room_id,
        thread_id=thread_id,
        known_latest_thread_event_id=known_latest_thread_event_id,
    )
    for attachment in attachments:
        if isinstance(attachment, AttachmentRecord):
            attachment_event_id = await send_file_message(
                context.client,
                room_id,
                attachment.local_path,
                filename=attachment.filename,
                mimetype=attachment.mime_type,
                thread_id=thread_id,
                latest_thread_event_id=latest_thread_event_id,
                extra_content={SKIP_MENTIONS_KEY: True},
            )
        else:
            attachment_event_id = await send_runtime_encrypted_media_message(
                context.client,
                room_id,
                attachment,
                thread_id=thread_id,
                latest_thread_event_id=latest_thread_event_id,
                extra_content={SKIP_MENTIONS_KEY: True},
            )
        if attachment_event_id is None:
            return attachment_event_ids, f"Failed to send attachment: {attachment.attachment_id}"
        attachment_event_ids.append(attachment_event_id)
        latest_thread_event_id = attachment_event_id
    return attachment_event_ids, None


class AttachmentTools(Toolkit):
    """Toolkit for reading and sending context-scoped attachments."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        workspace_in_worker: bool = False,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
    ) -> None:
        self._runtime_paths = runtime_paths
        self._worker_target = worker_target
        self._file_access = file_access
        # Whether the agent's workspace-path tools run in a worker, away from runtime attachment storage.
        self._workspace_in_worker = workspace_in_worker
        self._tool_output_workspace_root = tool_output_workspace_root
        super().__init__(
            name="attachments",
            tools=[
                self.list_attachments,
                self.get_attachment,
                self.register_attachment,
                self.view_file,
            ],
        )

    async def view_file(self, path: str) -> ToolValue:
        """View an image in your own model context without posting or invoking another model.

        To view an attachment ID, use get_attachment with view instead.
        PNG, JPEG, GIF and WebP images are supported, up to 20 MiB and 40 million pixels.
        Large images are resized to 2048 pixels; animation uses its first frame, with disclosure.
        The source path and a reusable attachment handle are retained when available.
        Use read_file for ordinary text/code; use matrix_message only when sharing is requested.
        """
        context = get_tool_runtime_context()
        if context is None:
            return media_error("Tool runtime context is unavailable.", metadata={"tool": "view_file"})
        if self._workspace_in_worker:
            try:
                result = await asyncio.to_thread(
                    view_file_from_worker,
                    runtime_paths=self._runtime_paths or context.runtime_paths,
                    runtime_config=context.config,
                    worker_target=self._worker_target,
                    workspace_root=self._tool_output_workspace_root,
                    path=path,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                result = media_error(str(exc), metadata={"tool": "view_file", "path": path})
        else:
            result = await asyncio.to_thread(
                view_agent_image,
                path,
                workspace=self._tool_output_workspace_root,
                file_access=self._file_access,
            )
        return await asyncio.to_thread(finalize_tool_media, result)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Offer ``get_attachment``'s output path as every tool offers one, and describe its attachment ID."""
        if function.name != "get_attachment":
            return function
        parameters = with_optional_output_path(function.parameters)
        parameters["properties"]["attachment_id"] = {
            **parameters["properties"]["attachment_id"],
            "description": "Context-scoped attachment ID returned by list_attachments.",
        }
        return replace(function, parameters=parameters)

    async def list_attachments(self, target: str | None = None) -> str:
        """List attachment metadata for current tool context."""
        context = get_tool_runtime_context()
        if context is None:
            return _attachment_tool_payload(
                "error",
                message="Tool runtime context is unavailable in this runtime path.",
            )

        requested_attachment_ids, attachments, missing_attachment_ids, error = _get_attachment_listing(
            context,
            target,
            tools_use_worker=self._workspace_in_worker,
        )
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
        view: bool = False,
    ) -> str | ToolValue:
        """Inspect metadata, save a file, or send attachment content to the model.

        Args:
            attachment_id: Context-scoped ID from list_attachments or register_attachment.
            mindroom_output_path: Save bytes to a workspace-relative file instead of returning metadata.
            view: Send image, audio, video, or document content (including PDF) to the model; up to 20 MiB.
                Images use the same preparation, limits, and replay behavior as view_file.
                Requires a model that supports the media type. If inline media is unavailable, use
                get_attachment without view to get metadata or save the file, then use other available tools.
                Cannot be combined with mindroom_output_path.

        """
        context = get_tool_runtime_context()
        if context is None:
            return _attachment_tool_payload(
                "error",
                message="Tool runtime context is unavailable in this runtime path.",
            )
        requested_attachment_id = attachment_id.strip()
        if not requested_attachment_id:
            return _attachment_tool_payload("error", message="attachment_id must be a non-empty string.")
        if view and mindroom_output_path is not None:
            return _attachment_tool_payload("error", message="view cannot be combined with mindroom_output_path.")

        _, attachments, missing_attachment_ids, error = _get_attachment_listing(
            context,
            requested_attachment_id,
            tools_use_worker=self._workspace_in_worker,
        )
        if error is not None:
            return _attachment_tool_payload("error", message=error)
        # One requested ID either resolves to one attachment or is reported missing.
        if missing_attachment_ids:
            return _attachment_tool_payload(
                "error",
                attachment_id=requested_attachment_id,
                message=f"Attachment metadata not found: {requested_attachment_id}",
            )
        if mindroom_output_path is not None:
            return await self._save_attachment_to_output_path(
                context,
                requested_attachment_id=requested_attachment_id,
                output_path=mindroom_output_path,
            )

        metadata: dict[str, object] = {
            "status": "ok",
            "tool": "attachments",
            "attachment_id": requested_attachment_id,
            "attachment": attachments[0],
        }
        if view:
            result = await asyncio.to_thread(
                _view_attachment,
                context,
                requested_attachment_id,
                metadata=metadata,
            )
            receipt = json.loads(result.content)
            if receipt.get("view_status") == "error":
                return _attachment_tool_payload(
                    "error",
                    attachment_id=requested_attachment_id,
                    message=receipt["message"],
                )
            return await asyncio.to_thread(finalize_tool_media, result)
        return json.dumps(metadata, sort_keys=True)

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

    async def _save_attachment_to_output_path(  # noqa: PLR0911
        self,
        context: ToolRuntimeContext,
        *,
        requested_attachment_id: str,
        output_path: str,
    ) -> str:
        use_worker = self._workspace_in_worker
        runtime_paths = self._runtime_paths or context.runtime_paths
        local_policy = (
            ToolOutputFilePolicy.from_runtime(self._tool_output_workspace_root, runtime_paths)
            if self._tool_output_workspace_root is not None
            else None
        )
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

        if use_worker:
            byte_limit = inline_attachment_byte_limit(runtime_paths)
            limit_label = "inline worker-transfer"
        else:
            # Without a worker, the path validation above already required a local workspace.
            assert local_policy is not None
            byte_limit = local_policy.max_bytes
            limit_label = "tool output redirect"
        payload_bytes, read_error = _read_attachment_bytes(
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
                    runtime_config=context.config,
                    worker_target=self._worker_target,
                    workspace_root=self._tool_output_workspace_root,
                    attachment_id=requested_attachment_id,
                    mindroom_output_path=output_path,
                    payload_bytes=payload_bytes,
                )
            except Exception as exc:
                return _attachment_tool_payload("error", attachment_id=requested_attachment_id, message=str(exc))
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
                mindroom_tool_output=saved_tool_output_receipt(
                    path=worker_receipt.worker_path,
                    byte_count=worker_receipt.size_bytes,
                    output_format="binary",
                    sha256=worker_receipt.sha256,
                ),
            )

        assert local_policy is not None
        write_result = write_bytes_to_output_path(local_policy, output_path, payload_bytes, file_mode=0o600)
        if isinstance(write_result, str):
            return _attachment_tool_payload("error", attachment_id=requested_attachment_id, message=write_result)

        attachment_payload.update(
            {
                "save_path": output_path,
                "size_bytes": write_result.byte_count,
                "sha256": sha256,
            },
        )
        return _attachment_tool_payload(
            "ok",
            attachment_id=requested_attachment_id,
            attachment=attachment_payload,
            mindroom_tool_output=saved_tool_output_receipt(
                path=output_path,
                byte_count=write_result.byte_count,
                output_format="binary",
                overwritten=write_result.overwritten,
                sha256=sha256,
            ),
        )

    async def register_attachment(self, file_path: str) -> str:
        """Register a local file as a context attachment ID.

        Paths resolve from the agent workspace and must stay inside it.
        """
        context = get_tool_runtime_context()
        if context is None:
            return _attachment_tool_payload(
                "error",
                message="Tool runtime context is unavailable in this runtime path.",
            )
        file_path = file_path.strip()
        if not file_path:
            return _attachment_tool_payload("error", message="file_path must be a non-empty string.")

        attachment_record, register_error = _register_attachment_file_path(
            context,
            file_path,
            workspace_root=self._tool_output_workspace_root,
            file_access=self._file_access,
        )
        if register_error is not None or attachment_record is None:
            return _attachment_tool_payload(
                "error",
                message=register_error or "Failed to register attachment file.",
            )

        return _attachment_tool_payload(
            "ok",
            attachment_id=attachment_record.attachment_id,
            attachment=_attachment_record_payload(attachment_record, tools_use_worker=self._workspace_in_worker),
        )
