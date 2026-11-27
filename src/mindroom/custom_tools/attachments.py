"""Attachment toolkit for context-scoped file discovery and sending."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.attachments.storage import (
    AttachmentRecord,
    attachments_for_tool_payload,
    filter_attachments_for_context,
    load_attachment,
    register_local_attachment,
)
from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.constants import SKIP_MENTIONS_KEY, SUPPRESS_PUSH_KEY
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.file_access import resolve_agent_file
from mindroom.llm.media import Audio, File, Video, is_document_mime_type, named_mime_type
from mindroom.matrix.client_delivery import (
    DeliveredMatrixEvent,
    MatrixDeliveryFailure,
    MatrixDeliveryFailureKind,
    resolve_room_encryption_outcome,
    send_file_message_outcome,
    send_runtime_encrypted_media_message,
    upload_media_bytes_as_mxc_outcome,
)
from mindroom.matrix.inline_media import register_inline_media
from mindroom.matrix.media import resolve_image_mime_type
from mindroom.matrix.runtime_media import RuntimeEncryptedMediaAttachment
from mindroom.media_delivery import MAX_SOURCE_BYTES, image_result, media_error, view_agent_image
from mindroom.path_confinement import open_regular_file_within_root
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.media_attachments import finalize_tool_media
from mindroom.tool_system.output_files import (
    ToolOutputFilePolicy,
    ToolOutputFileRequest,
    current_tool_workdir,
    finalize_tool_output_file,
    normalize_output_path_argument,
    prepare_tool_output_file,
    saved_tool_output_receipt,
    validate_output_path,
    validate_output_path_syntax,
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
    'Not in your workspace. Copy it there with attachment(action="get", attachment_id=..., mindroom_output_path=...) '
    "before filesystem, python, or shell tools use it."
)


def _attachment_tool_payload(status: str, **kwargs: object) -> str:
    return custom_tool_payload("attachments", status, **kwargs)


def _infer_local_attachment_metadata(local_path: Path) -> tuple[_LocalAttachmentKind, str | None]:
    """Infer the attachment kind and MIME type of a local file registered by path."""
    mime_type = named_mime_type(local_path.name)
    if mime_type is None:
        return "file", None
    media_type = mime_type.split("/", 1)[0].lower()
    if media_type in {"audio", "image", "video"}:
        return cast("_LocalAttachmentKind", media_type), mime_type
    return "file", mime_type


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
    attachment = resolve_context_attachment_record(context, attachment_id)
    if isinstance(attachment, str):
        return None, attachment
    return attachment.local_path, None


def read_context_attachment(
    context: ToolRuntimeContext,
    attachment_id: str,
    *,
    byte_limit: int,
    limit_label: str,
) -> tuple[bytes | None, str | None]:
    """Read the bytes of an attachment available in this conversation, or return why it cannot be read; blocking."""
    attachment = resolve_context_attachment_record(context, attachment_id)
    if isinstance(attachment, str):
        return None, attachment
    return _read_attachment_bytes(attachment, byte_limit=byte_limit, limit_label=limit_label)


def resolve_context_attachment_record(
    context: ToolRuntimeContext,
    attachment_id: str,
) -> AttachmentRecord | str:
    """Resolve a context attachment ID to a readable attachment record, or an error."""
    if context.storage_path is None:
        return "Attachment storage path is unavailable in this runtime path."
    if not attachment_id_available_in_tool_runtime_context(context, attachment_id):
        return f"Attachment ID is not available in this context: {attachment_id}"
    if get_tool_runtime_media_attachment(context, attachment_id) is not None:
        return f"Ephemeral attachment {attachment_id} can be sent but has no local file."

    attachment = load_attachment(context.storage_path, attachment_id)
    if attachment is None:
        return f"Attachment metadata not found: {attachment_id}"
    if not attachment.local_path.is_file():
        return f"Attachment file is missing on disk: {attachment_id}"
    return attachment


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
    attachment = resolve_context_attachment_record(context, attachment_id)
    if isinstance(attachment, str):
        return media_error(attachment, metadata=metadata)
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
        # The turn's own attachments were scoped when it was given them, vetted fork parents included.
        in_vetted_fork_parent = (
            attachment.room_id is not None
            and attachment.thread_id is not None
            and (attachment.room_id, attachment.thread_id) in context.fork_parent_scopes
        )
        given = attachment.attachment_id in context.attachment_ids and (
            attachment.room_id == context.room_id or in_vetted_fork_parent
        )
        if not (allowed or given):
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
    if is_document_mime_type(mime_type):
        return ToolValue(
            content=content,
            files=(File(content=payload, mime_type=mime_type, filename=filename, format=media_format),),
        )
    return media_error(
        'This file type cannot be sent as model media. Use attachment(action="get", attachment_id=...) without view to inspect or save it.',
        metadata=metadata,
    )


def _register_attachment_file_path(
    context: ToolRuntimeContext,
    file_path: str,
    *,
    workspace_root: Path | None,
    file_access: FileAccess,
) -> AttachmentRecord | str:
    """Register a local file path in the current tool context, or return an error."""
    if context.storage_path is None:
        return "Attachment storage path is unavailable in this runtime path."

    # The path is model-supplied and reaches an open in the primary process, so
    # workspace access confines it to the workspace and fails closed without one;
    # only already authorized ``att_*`` IDs then remain sendable. Registration
    # copies the file below the authorized root without following links.
    try:
        authorized = resolve_agent_file(
            file_path,
            workspace_root=workspace_root,
            file_access=file_access,
            field_name="attachment file path",
        )
    except ValueError as exc:
        return str(exc)
    resolved_path = authorized.root / authorized.relative
    kind, mime_type = _infer_local_attachment_metadata(resolved_path)
    attachment_record = register_local_attachment(
        context.storage_path,
        resolved_path,
        kind=kind,
        source_root=authorized.root,
        mime_type=mime_type,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id,
        sender=context.requester_id,
    )
    if attachment_record is None:
        return f"Failed to register attachment file: {resolved_path}"

    append_tool_runtime_attachment_id(attachment_record.attachment_id)
    return attachment_record


def _resolve_attachment_source(
    context: ToolRuntimeContext,
    *,
    attachment_id: str | None,
    path: str | None,
    workspace_root: Path | None,
    file_access: FileAccess,
) -> _ResolvedSendAttachment | str:
    """Resolve a sendable attachment strictly as either a context attachment ID or a local file path."""
    if attachment_id is not None:
        runtime_media = get_tool_runtime_media_attachment(context, attachment_id)
        if runtime_media is not None:
            return runtime_media
        return resolve_context_attachment_record(context, attachment_id)
    assert path is not None
    return _register_attachment_file_path(
        context,
        path,
        workspace_root=workspace_root,
        file_access=file_access,
    )


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
    is_id = reference.startswith("att_")
    record = _resolve_attachment_source(
        context,
        attachment_id=reference if is_id else None,
        path=None if is_id else reference,
        workspace_root=workspace_root,
        file_access=file_access,
    )
    if isinstance(record, str):
        return record
    return record, not is_id


_ALLOWED_ATTACHMENT_PARAMS: dict[str, frozenset[str]] = {
    "list": frozenset({"attachment_id"}),
    "get": frozenset({"attachment_id", "view"}),
    "register": frozenset({"path"}),
    "inline_image": frozenset({"attachment_id", "path", "alt"}),
}


@dataclass(frozen=True, slots=True)
class _NormalizedAttachmentArgs:
    attachment_id: str | None
    path: str | None
    alt: str | None
    mindroom_output_path: str | None


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _parse_attachment_args(  # noqa: C901, PLR0911
    action: str,
    *,
    attachment_id: str | None,
    path: str | None,
    view: bool,
    alt: str | None,
    mindroom_output_path: str | None,
) -> _NormalizedAttachmentArgs | str:
    """Normalize and validate parameters for ``action`` once before any side effects."""
    allowed = _ALLOWED_ATTACHMENT_PARAMS.get(action)
    if allowed is None:
        return f"Unsupported attachment action {action!r}. Expected one of: list, get, register, inline_image."
    # Treat empty/whitespace-only strings on irrelevant optional parameters as unset so callers emitting "" for unused
    # schema fields do not get spurious errors; allowed/required parameters still validate blank values below.
    resolved_id = _non_empty(attachment_id)
    resolved_path = _non_empty(path)
    resolved_alt = alt if alt is not None and alt.strip() else None
    # On action="get", keep explicitly passed blank mindroom_output_path (e.g. "") so unsafe-path validation rejects it
    # consistently with view=True and get_attachment; on other actions, blank strings act as unset like output_files.
    raw_output_path = mindroom_output_path if action == "get" else normalize_output_path_argument(mindroom_output_path)
    resolved_output_path = raw_output_path if isinstance(raw_output_path, str) else None
    provided = (
        ("attachment_id", resolved_id is not None),
        ("path", resolved_path is not None),
        ("view", view),
        ("alt", resolved_alt is not None),
    )
    unexpected = [name for name, is_set in provided if is_set and name not in allowed]
    if unexpected:
        names = ", ".join(f"'{name}'" for name in unexpected)
        return f"Parameter(s) {names} cannot be used with action={action!r}."
    if action == "get":
        if resolved_id is None:
            return "attachment_id must be a non-empty string."
        if view and resolved_output_path is not None:
            return "view cannot be combined with mindroom_output_path."
    elif action == "register":
        if resolved_path is None:
            return "path must be a non-empty string."
    elif action == "inline_image":
        if resolved_id is not None and resolved_path is not None:
            return "Pass either attachment_id or path for action='inline_image', not both."
        if resolved_id is None and resolved_path is None:
            if attachment_id is not None and path is None:
                return "attachment_id must be a non-empty string."
            if path is not None and attachment_id is None:
                return "path must be a non-empty string."
            return "action='inline_image' requires either attachment_id or path."
    return _NormalizedAttachmentArgs(
        attachment_id=resolved_id,
        path=resolved_path,
        alt=resolved_alt,
        mindroom_output_path=resolved_output_path,
    )


def _too_large_detail(outcome: object) -> str | None:
    if isinstance(outcome, MatrixDeliveryFailure) and outcome.kind is MatrixDeliveryFailureKind.PAYLOAD_TOO_LARGE:
        return outcome.detail
    return None


async def send_resolved_attachments(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    thread_id: str | None,
    attachments: list[_ResolvedSendAttachment],
    known_latest_thread_event_id: str | None = None,
    suppress_push: bool = False,
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
    extra_content: dict[str, Any] = {SKIP_MENTIONS_KEY: True}
    if suppress_push:
        extra_content[SUPPRESS_PUSH_KEY] = True
    for attachment in attachments:
        if isinstance(attachment, AttachmentRecord):
            outcome = await send_file_message_outcome(
                context.client,
                room_id,
                attachment.local_path,
                filename=attachment.filename,
                mimetype=attachment.mime_type,
                thread_id=thread_id,
                latest_thread_event_id=latest_thread_event_id,
                extra_content=dict(extra_content),
            )
            if (too_large := _too_large_detail(outcome)) is not None:
                return attachment_event_ids, too_large
            attachment_event_id = outcome.event_id if isinstance(outcome, DeliveredMatrixEvent) else None
        else:
            attachment_event_id = await send_runtime_encrypted_media_message(
                context.client,
                room_id,
                attachment,
                thread_id=thread_id,
                latest_thread_event_id=latest_thread_event_id,
                extra_content=dict(extra_content),
            )
        if attachment_event_id is None:
            return attachment_event_ids, f"Failed to send attachment: {attachment.attachment_id}"
        attachment_event_ids.append(attachment_event_id)
        latest_thread_event_id = attachment_event_id
    return attachment_event_ids, None


class AttachmentTools(Toolkit):
    """Toolkit for reading and sending context-scoped attachments."""

    offer_background_after = False
    offer_output_path = False

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths | None = None,
        runtime_config: Config | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        workspace_in_worker: bool = False,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
    ) -> None:
        self._runtime_paths = runtime_paths
        self._runtime_config = runtime_config
        self._worker_target = worker_target
        self._file_access = file_access
        # Whether the agent's workspace-path tools run in a worker, away from runtime attachment storage.
        self._workspace_in_worker = workspace_in_worker
        self._tool_output_workspace_root = tool_output_workspace_root
        # view_file reads where the file tools work: a headless run's workdir, else the workspace.
        self._view_root = current_tool_workdir() or tool_output_workspace_root
        super().__init__(
            name="attachments",
            tools=[
                self.attachment,
                self.view_file,
            ],
        )

    def views(self, path: str) -> bool:
        """Return whether view_file finds ``path`` as a file it may read.

        A worker resolves the path itself, under workspace access only inside the workspace view_file sends it to.
        """
        if self._workspace_in_worker:
            requested = Path(path)
            workspace = self._tool_output_workspace_root
            return (
                self._file_access == "unrestricted"
                or not requested.is_absolute()
                or (workspace is not None and requested.is_relative_to(workspace))
            )
        try:
            resolve_agent_file(path, workspace_root=self._view_root, file_access=self._file_access, field_name="path")
        except ValueError:
            return False
        return True

    async def view_file(self, path: str) -> ToolValue:
        """View an image in your own model context without posting or invoking another model.

        To view an attachment ID, use attachment(action="get", attachment_id=..., view=True) instead.
        PNG, JPEG, GIF and WebP images are supported, up to 20 MiB and 40 million pixels.
        Large images are resized to 2048 pixels; animation uses its first frame, with disclosure.
        The source path and a reusable attachment handle are retained when available.
        Use read_file for ordinary text/code; use matrix_message only when sharing is requested.
        """
        if self._workspace_in_worker:
            assert self._runtime_paths is not None, "an agent build gives every toolkit its runtime paths"
            try:
                result = await asyncio.to_thread(
                    view_file_from_worker,
                    runtime_paths=self._runtime_paths,
                    runtime_config=self._runtime_config,
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
                workspace=self._view_root,
                file_access=self._file_access,
            )
        return await asyncio.to_thread(finalize_tool_media, result)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Offer ``attachment``'s output path and wait budget for binary saves and JSON redirects."""
        if function.name != "attachment":
            return function
        return replace(function, offer_background_after=True, offer_output_path=True)

    def _prepare_output_request(
        self,
        context: ToolRuntimeContext,
        output_path: str | None,
    ) -> ToolOutputFileRequest | dict[str, object] | str | None:
        """Prepare the shared output-file request matching ``get_tool_by_name`` whenever a workspace is configured."""
        if self._tool_output_workspace_root is None:
            if output_path is not None:
                return _attachment_tool_payload(
                    "error",
                    message="mindroom_output_path requires an agent workspace in this runtime path.",
                )
            return None
        runtime_paths = self._runtime_paths or context.runtime_paths
        config = self._runtime_config or context.config
        policy = ToolOutputFilePolicy.for_current_tools(
            self._tool_output_workspace_root,
            runtime_paths,
            auto_save_threshold_bytes=config.defaults.tool_output_auto_save_threshold_bytes,
        )
        return prepare_tool_output_file(policy, tool_name="attachment", output_path=output_path)

    async def _dispatch_attachment_action(
        self,
        action: Literal["list", "get", "register", "inline_image"],
        context: ToolRuntimeContext,
        parsed: _NormalizedAttachmentArgs,
        *,
        view: bool,
    ) -> str | ToolValue:
        if action == "list":
            return self._list_attachments(context, parsed.attachment_id)
        if action == "get":
            assert parsed.attachment_id is not None
            return await self._get_attachment(
                context,
                parsed.attachment_id,
                mindroom_output_path=parsed.mindroom_output_path,
                view=view,
            )
        if action == "register":
            assert parsed.path is not None
            return self._register_attachment(context, parsed.path)
        return await self._inline_image(
            context,
            attachment_id=parsed.attachment_id,
            path=parsed.path,
            alt=parsed.alt,
        )

    async def attachment(
        self,
        action: Literal["list", "get", "register", "inline_image"],
        attachment_id: str | None = None,
        path: str | None = None,
        view: bool = False,
        alt: str | None = None,
        mindroom_output_path: str | None = None,
    ) -> str | dict[str, object] | ToolValue:
        """Manage conversation attachments.

        - `list`: list context attachments (optional `attachment_id` filter).
        - `get`: inspect `attachment_id` metadata, save raw bytes to `mindroom_output_path`, or pass `view=True` to load image/audio/video/document (<=20 MiB) into model context.
        - `register`: register a local file at `path` as a context `attachment_id`.
        - `inline_image`: upload an image by `attachment_id` or `path` (not both; optional `alt`) and return `markdown` for a reply or `matrix_message` (on refusal, use `matrix_message(attachments=[...])`; never use placeholder URLs).

        Args:
            action: Operation to perform.
            attachment_id: Context attachment ID (list filter, get, or inline_image).
            path: Local file path (register or inline_image).
            view: Load media into model context (get only; incompatible with mindroom_output_path).
            alt: Markdown alt text (inline_image).
            mindroom_output_path: Optional workspace-relative path: save raw bytes (get) or JSON output to file and return a receipt.

        """
        parsed = _parse_attachment_args(
            action,
            attachment_id=attachment_id,
            path=path,
            view=view,
            alt=alt,
            mindroom_output_path=mindroom_output_path,
        )
        if isinstance(parsed, str):
            return _attachment_tool_payload("error", message=parsed)
        context = get_tool_runtime_context()
        if context is None:
            return _attachment_tool_payload(
                "error",
                message="Tool runtime context is unavailable in this runtime path.",
            )
        owns_output = action == "get" and (parsed.mindroom_output_path is not None or view)
        prepared = None if owns_output else self._prepare_output_request(context, parsed.mindroom_output_path)
        if isinstance(prepared, (dict, str)):
            return prepared
        result = await self._dispatch_attachment_action(action, context, parsed, view=view)
        if prepared is not None and isinstance(result, str):
            finalized = finalize_tool_output_file(prepared, result)
            return finalized if isinstance(finalized, (str, dict)) else result
        return result

    def _list_attachments(self, context: ToolRuntimeContext, target: str | None) -> str:
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

    async def _get_attachment(
        self,
        context: ToolRuntimeContext,
        requested_attachment_id: str,
        *,
        mindroom_output_path: str | None,
        view: bool,
    ) -> str | ToolValue:
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

    async def _save_attachment_to_output_path(
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
        # Validate the requested output path before reading attachment bytes.
        if use_worker:
            path_error = validate_output_path_syntax(output_path)
        elif local_policy is not None:
            path_error = validate_output_path(local_policy, output_path)
        else:
            path_error = "mindroom_output_path requires an agent workspace in this runtime path."
        if path_error is not None:
            return _attachment_tool_payload("error", attachment_id=requested_attachment_id, message=path_error)

        attachment = resolve_context_attachment_record(context, requested_attachment_id)
        if isinstance(attachment, str):
            return _attachment_tool_payload("error", attachment_id=requested_attachment_id, message=attachment)

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

        attachment_payload = attachments_for_tool_payload([attachment])[0]
        attachment_payload.pop("local_path", None)

        overwritten: bool | None
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
            save_path, byte_count, sha256, overwritten = (
                worker_receipt.worker_path,
                worker_receipt.size_bytes,
                worker_receipt.sha256,
                None,
            )
        else:
            assert local_policy is not None
            write_result = write_bytes_to_output_path(local_policy, output_path, payload_bytes, file_mode=0o600)
            if isinstance(write_result, str):
                return _attachment_tool_payload("error", attachment_id=requested_attachment_id, message=write_result)
            save_path, byte_count, sha256, overwritten = (
                output_path,
                write_result.byte_count,
                hashlib.sha256(payload_bytes).hexdigest(),
                write_result.overwritten,
            )

        attachment_payload.update({"save_path": save_path, "size_bytes": byte_count, "sha256": sha256})
        return _attachment_tool_payload(
            "ok",
            attachment_id=requested_attachment_id,
            attachment=attachment_payload,
            mindroom_tool_output=saved_tool_output_receipt(
                path=save_path,
                byte_count=byte_count,
                output_format="binary",
                overwritten=overwritten,
                sha256=sha256,
            ),
        )

    def _register_attachment(self, context: ToolRuntimeContext, requested_path: str) -> str:
        attachment_record = _register_attachment_file_path(
            context,
            requested_path,
            workspace_root=self._tool_output_workspace_root,
            file_access=self._file_access,
        )
        if isinstance(attachment_record, str):
            return _attachment_tool_payload("error", message=attachment_record)
        return _attachment_tool_payload(
            "ok",
            attachment_id=attachment_record.attachment_id,
            attachment=_attachment_record_payload(attachment_record, tools_use_worker=self._workspace_in_worker),
        )

    @staticmethod
    def _inline_runtime_image(
        room_id: str,
        record: RuntimeEncryptedMediaAttachment,
        *,
        room_encrypted: bool,
    ) -> tuple[str, str] | str:
        if not room_encrypted:
            return "Inline images in unencrypted rooms need plaintext media, not encrypted Matrix media."
        if record.size > MAX_SOURCE_BYTES:
            return f"Attachment is too large to use as an inline image ({record.size} bytes; max {MAX_SOURCE_BYTES})."
        register_inline_media(room_id, record.url, record.encrypted_file_content())
        return record.url, record.filename

    async def _inline_image(  # noqa: C901, PLR0911
        self,
        context: ToolRuntimeContext,
        *,
        attachment_id: str | None,
        path: str | None,
        alt: str | None,
    ) -> str:
        room_encrypted = await resolve_room_encryption_outcome(
            context.client,
            context.room_id,
            operation="inline_image",
        )
        if isinstance(room_encrypted, MatrixDeliveryFailure):
            return _attachment_tool_payload(
                "error",
                message="Inline images cannot be sent to this room right now. Send the image as an attachment with matrix_message instead.",
            )
        resolved = await asyncio.to_thread(
            _resolve_attachment_source,
            context,
            attachment_id=attachment_id,
            path=path,
            workspace_root=self._tool_output_workspace_root,
            file_access=self._file_access,
        )
        if isinstance(resolved, str):
            return _attachment_tool_payload("error", message=resolved)
        record = resolved
        if not record.mime_type or not record.mime_type.startswith("image/"):
            return _attachment_tool_payload(
                "error",
                message="Inline images require an image attachment (image/* MIME type).",
            )
        if isinstance(record, RuntimeEncryptedMediaAttachment):
            result = self._inline_runtime_image(context.room_id, record, room_encrypted=room_encrypted)
            if isinstance(result, str):
                return _attachment_tool_payload("error", message=result)
            mxc_uri, filename = result
        else:
            data, error = await asyncio.to_thread(
                _read_attachment_bytes,
                record,
                byte_limit=MAX_SOURCE_BYTES,
                limit_label="inline image",
            )
            if error is not None or data is None:
                return _attachment_tool_payload("error", message=error or "Attachment is unavailable.")
            mime_type = resolve_image_mime_type(data, record.mime_type).detected_mime_type
            if mime_type is None:
                return _attachment_tool_payload("error", message="Attachment bytes are not a recognized image.")
            filename = record.filename or record.local_path.name
            upload_outcome = await upload_media_bytes_as_mxc_outcome(
                context.client,
                context.room_id,
                data,
                filename=filename,
                mimetype=mime_type,
            )
            if isinstance(upload_outcome, MatrixDeliveryFailure):
                return _attachment_tool_payload(
                    "error",
                    message=_too_large_detail(upload_outcome) or "Failed to upload inline image.",
                )
            mxc_uri, upload_payload = upload_outcome
            if room_encrypted or "file" in upload_payload:
                encrypted_file = upload_payload.get("file")
                if not isinstance(encrypted_file, dict) or encrypted_file.get("url") != mxc_uri:
                    return _attachment_tool_payload("error", message="Failed to upload encrypted inline image.")
                register_inline_media(context.room_id, mxc_uri, encrypted_file)
        label = " ".join((alt or filename).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").splitlines())
        return _attachment_tool_payload(
            "ok",
            attachment_id=record.attachment_id,
            mxc_uri=mxc_uri,
            markdown=f"![{label}]({mxc_uri})",
        )
