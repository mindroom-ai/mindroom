"""Bounded CLI projections using the existing workspace and attachment owners."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from agno.media import File

from mindroom.attachments import register_local_attachment
from mindroom.tool_system.output_files import (
    ToolOutputFileRequest,
    finalize_tool_output_file,
    write_bytes_to_output_path,
)

if TYPE_CHECKING:
    from agno.models.response import ModelResponse

    from mindroom.tool_system.output_files import ToolOutputFilePolicy
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def project_cli_result(result: object, policy: ToolOutputFilePolicy | None, tool_name: str) -> object:
    """Save large text through the normal output policy without changing execution status."""
    if policy is None:
        return result
    # ToolExecution already separated media; only project its textual result.
    # Budget encoded JSON: control characters can expand sixfold on the wire.
    inline_budget = 16 * 1024
    threshold = min(policy.auto_save_threshold_bytes, inline_budget)
    if isinstance(result, str) and len(json.dumps(result, ensure_ascii=False).encode()) > inline_budget:
        threshold = 0
    bounded = replace(policy, auto_save_threshold_bytes=threshold)
    projected = finalize_tool_output_file(ToolOutputFileRequest(bounded, tool_name, None), result)
    if isinstance(projected, dict):
        output = cast("dict[str, object]", projected).get("mindroom_tool_output")
        if isinstance(output, dict):
            receipt = cast("dict[str, object]", output)
            preview = receipt.get("preview")
            if isinstance(preview, str):
                # The writer's raw 8 KiB preview can also expand. Shorten only this
                # display copy; the artifact already contains the exact full output.
                while preview and len(json.dumps(projected, ensure_ascii=False).encode()) > inline_budget:
                    preview = preview[: len(preview) // 2]
                    receipt["preview"] = f"{preview}\n[Preview shortened for CLI. Full tool output was saved to file.]"
    return projected


def register_cli_media(
    response: ModelResponse,
    *,
    context: ToolRuntimeContext,
    policy: ToolOutputFilePolicy | None,
    call_id: str,
) -> list[dict[str, object]]:
    """Retain scoped local references while leaving Agno's media objects intact."""
    references: list[dict[str, object]] = []
    for kind, items in (
        ("image", response.images),
        ("audio", response.audios),
        ("video", response.videos),
        ("file", response.files),
    ):
        for item in items or []:
            reference: dict[str, object] = {"kind": kind, "mime_type": item.mime_type}
            if item.url:
                reference["url"] = item.url
                references.append(reference)
                continue
            filepath = item.filepath
            content = item.content
            filename = item.filename if isinstance(item, File) else None
            path = Path(filepath) if filepath else None
            if path is None and isinstance(content, bytes) and policy is not None:
                relative = f"mindroom_tool_outputs/media-{uuid4().hex}"
                saved = write_bytes_to_output_path(policy, relative, content)
                if not isinstance(saved, str):
                    path = saved.absolute_path
                    reference["path"] = relative
            if path is not None and context.storage_path is not None:
                record = register_local_attachment(
                    context.storage_path,
                    path,
                    kind=kind,
                    filename=filename or path.name,
                    mime_type=item.mime_type,
                    room_id=context.room_id,
                    thread_id=context.resolved_thread_id,
                    source_event_id=call_id,
                    sender=context.requester_id,
                )
                if record is not None:
                    reference["attachment_id"] = record.attachment_id
                    if record.attachment_id not in context.runtime_attachment_ids:
                        context.runtime_attachment_ids.append(record.attachment_id)
            if "attachment_id" not in reference:
                reference["error"] = "Attachment reference unavailable; media retained in Bash result"
            references.append(reference)
    return references


def schema_context_document(result: dict[str, object]) -> str:
    """Serialize a complete authorized descriptor for the existing paged context reader."""
    return json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
